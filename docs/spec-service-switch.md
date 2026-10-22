# ТЗ: ServiceSwitch — control plane для GPU/CPU-сервисов

> **Статус:** черновик к реализации (v1.0).
> **Репозиторий:** `projects/Switch-Service` (сейчас пустой: `LICENSE`, `.gitignore`, пустой `README.md`).
> **Меняет:** `Auth-Service` (`deploy/gateway.yaml`, `web/src/pages/Admin.tsx`, `web/src/api.ts`, `web/src/types.ts`),
> `RAG-Chat` (`docker-compose.yml`, `services/embedding`), `qwen-image-service` (`deploy/qwen-image.container`),
> `llama.cpp` (startup-скрипты — только как справочник, выкидываются из обихода).
> **Не меняет:** `User-Service` (роли остаются как есть; новых ролей не вводим), `WebStorage`, `HomeMessanger`.
> **Оператор:** один хост `master-node` (rootless Podman 5.7.0), один GPU `Tesla V100 32 GiB`.

---

## 0. Решение и почему не Jenkins

**Управляющий слой — сам ServiceSwitch.** Jenkins как control plane **не нужен** и здесь вреден.
Jenkins остаётся максимум как CI для сборки образов (и то опционально — сейчас образы собираются `podman build` из Makefile).

Почему:

| Требование из задачи | Jenkins | ServiceSwitch |
|---|---|---|
| «перед переключением проверить, есть ли место» | нет: build/job не умеет бронировать ресурсы, два параллельных job'а оба увидят «место есть» | аллокатор с мьютексом и резервированием на время старта |
| Атомарность + откат | нет: упавший `podman run` оставляет мусор, job «зелёный» или «красный», состояние сервиса неизвестно | транзакция: admission → create → start → health → commit, иначе rollback в прежнее состояние |
| Живые RAM/VRAM в админке | нет: только ссылка на job и его лог | `GET /api/services` раз в 5 с, цифры в UI |
| Boot-состояние | Jenkins должен быть запущен, чтобы вернуть сервисы | reconciler в самом switchd + `podman-restart` для boot |
| Права | нужен доступ к podman-сокету пользователя = root-эквивалент внутри хоста, плюс отдельная auth-поверхность и JVM ~1 GiB RAM | один сокет, один API, auth через уже существующий gateway (`auth: admin`) |
| Стоимость | `jenkins/jenkins` + плагины + обновления, ~1 GiB RAM на боксе, где RAM — дефицитный ресурс (qwen-image держит 33 GiB) | ~40 MiB RSS Go-бинарника |

**Итог:** ServiceSwitch — единственный писатель состояния контейнеров. Любой внешний триггер (CI, Jenkins, curl, кнопка в админке)
делает только `POST /api/services/{id}/state` и ничего не знает про podman. Инвариант: *никто, кроме switchd, не вызывает
`podman start/stop/run` для управляемых сервисов*.

Если Jenkins всё же нужен — см. §14: он подключается как тонкий клиент API, без сокета и без `systemctl`.

---

## 1. Что есть сейчас (замеры 2026-10-22, `master-node`)

### 1.1 Хост

| Параметр | Значение |
|---|---|
| RAM | 88 GiB total, `MemAvailable` 47 GiB в момент замера, swap 0 |
| CPU | 16 потоков |
| GPU 0 | `Tesla PG503-216` (V100, sm_70), 32768 MiB, занято 440 MiB в простое |
| Podman | 5.7.0 **rootless**, `Rootless=true`, storage `/home/aleksey/.local/share/containers/storage` |
| Linger | `Linger=yes` (юзер-юниты живут без логина) — switchd можно держать как `systemctl --user` |
| Сети | `auth_network`/`deploy_auth_network`, `llm_network`, `rag_internal`, `rag_edge`, `qwen_image_gen_network`, `common_network`, `user_network` |
| CDI | `nvidia.com/gpu=all` (так GPU прокидывается в контейнеры; compose `deploy.resources` на этом podman **не работает**) |

### 1.2 Управляемые сервисы (как запущены сегодня)

| # | Сервис | Контейнер | Сеть / DNS | Порт | Как стартует сейчас | Режимы сейчас |
|---|---|---|---|---|---|---|
| 1 | OCR (Surya OCR 2) | `rag-chat_ocr_1` | `rag_internal` / `ocr` | 8090 (+ llama-server на 127.0.0.1:8092 внутри) | `podman compose` (RAG-Chat) | **два разных образа**: `--target cpu` и `--target cuda` (`make up-gpu`), т.е. переключение = rebuild |
| 2 | Embedder (EmbeddingGemma 2 Q8_0) | `rag-chat_embedding_1` | `rag_internal` / `embedding` | 8091 (+ llama-server 8092) | `podman compose` (RAG-Chat) | один CPU-образ, GPU-флага нет вообще (`build_llama_server_command` не передаёт `-ngl`) |
| 3 | LLM (Qwen 3.8 Q8_0) | `qwen38` | `llm_network` / `qwen38` | 4001 | `podman run` из `llama.cpp/startup_qwen3.8_work_instance_podman.sh` | GPU-only, `-ngl -1`, ctx 128000 |
| 4 | Image Gen (Qwen-Image-2.1) | `qwen-image` | `qwen_image_gen_network` / `qwen-image`, + `deploy_auth_network` | 8000 | **quadlet** `~/.config/containers/systemd/qwen-image.container` (`Restart=always`) | GPU-only, `MEMORY_MODE=int8` |

### 1.3 Замеры памяти (факт, не оценка)

| Объект | RAM (RSS/cgroup) | VRAM | Источник |
|---|---|---|---|
| `qwen-image` (int8, модель загружена) | **33.14 GB** | 440 MiB в простое (ленивая загрузка; при генерации 20–28 GiB) | `podman stats`, `nvidia-smi`, README сервиса |
| `rag-chat_ocr_1` (CPU target, модель загружена) | **1.263 GB** | 0 | `podman stats` |
| `rag-chat_embedding_1` (модель загружена) | **630.7 MB** | 0 | `podman stats` |
| `qwen38` (не запущен в момент замера) | — | — | — |
| Прочие (auth/rag/webstorage/…) | суммарно ~1.5 GB | 0 | `podman stats` |

Размеры весов на диске (для расчёта claims):

| Файл | Байт | GiB |
|---|---|---|
| `surya-2.gguf` | 1 266 400 864 | 1.18 |
| `surya-2-mmproj.gguf` | 204 986 688 | 0.19 |
| `embeddinggemma-2-Q8_0.gguf` (+tokenizer) | ~343 000 000 | 0.32 |
| `model_q8.gguf` (Qwen 3.8) | 29 047 084 320 | **27.05** |
| `mmproj-F16.gguf` (Qwen 3.8 vision, для image-варианта) | 927 607 488 | 0.86 |
| Qwen-Image-2.1 (HF-кэш) | ~42 GB на диске | int8 VRAM ~20 GB по README |

### 1.4 Главный вывод из цифр

**V100 32 GiB не вмещает LLM и Image Gen одновременно.**
LLM = 27.05 GiB весов + KV + CUDA-контекст ≈ **29–30 GiB**; Image Gen int8 ≈ **20 GiB** + ~2 GiB KV на каждую референс-картинку.
Поэтому:

- `llm=gpu` **эксклюзивен** для GPU (рядом влезают только OCR/embedder, если урезать ctx, и то на грани);
- `image=gpu` совместим с `ocr=gpu` + `embedder=gpu` (20.5 + 3 + 1.5 ≈ 25 GiB);
- переключение «LLM → Image Gen» — это всегда **два** перехода (stop одного, start другого), и API обязан это планировать, а не падать с 409 без объяснения.

Второй вывод: **CPU/GPU — это runtime-флаг `-ngl`, а не два образа.** CUDA-сборка `llama-server` прекрасно работает с `-ngl 0`
(и с `-ngl N` для частичного оффлоада). Переключение образа на лету (как `make up-gpu`) занимает минуты на rebuild и
невозможно без исходников — это надо убрать.

---

## 2. Цели и не-цели

### Цели (v1)

1. Три состояния на сервис: `off` / `cpu` / `gpu`, переключение без пересборки образов.
2. Проверка бюджета RAM и VRAM до старта + резервирование на время перехода + внятный отказ, если не влезает.
3. Одна админка: секция **Services** на `filenkov.store/admin` рядом с существующей таблицей Directory; рядом с каждым сервисом — доступные RAM/VRAM.
4. Состояние переживает ребут и падение switchd (reconciler + персистентный desired state).
5. Полный аудит: кто, когда, из какого состояния в какое, чем закончилось, сколько заняло.
6. Никаких правок `Auth-Service` API, `User-Service`, ролей — только gateway-маршрут + SPA.

### Не-цели (v1)

- Мульти-GPU и удалённые хосты (`GPU_DEVICE_ID` учитываем как параметр каталога, но балансировки нет).
- Автоскейлинг по нагрузке/очереди, приоритеты, preemption по таймауту простоя.
- Сборка образов через API (`build` — вручную/CI, см. §14).
- Управление не-GPU сервисами (mongo, qdrant, gateway и т.п.) — только 4 сервиса из §1.2. `litellm-proxy-server` (сейчас `Exited`)
  в scope не входит, но каталог расширяемый.
- Замена `rag-edge`/gateway-маршрутизации: если сервис выключен, потребитель получает обычный 502/503 от своего upstream.

---

## 3. Архитектура

```
                              браузер (ADMIN)
                                    │  https://filenkov.store/admin
                                    ▼
                    ┌───────────────────────────────┐
                    │ Caddy → gateway :8083         │
                    │  /admin*      → web (SPA)     │
                    │  /api/services* → switch :8095│  auth: admin
                    └───────────────┬───────────────┘
                                    │ deploy_auth_network
                                    ▼
   ┌────────────────────────────────────────────────────────────┐
   │ switchd  (Go, rootless-контейнер, systemd --user)          │
   │  ├─ httpapi   REST + JSON                                  │
   │  ├─ catalog   config/services.yaml (профили off/cpu/gpu)   │
   │  ├─ alloc     RAM/VRAM admission, резервы, мьютекс         │
   │  ├─ state     SQLite: desired/actual, transitions, audit   │
   │  ├─ runtime   podman API (unix socket) + nvidia-smi        │
   │  ├─ health    HTTP-пробы контейнеров                       │
   │  └─ reconcile  каждые 15 с: desired == actual?             │
   └───────┬───────────────────────┬────────────────────────────┘
           │ /run/podman/podman.sock│ nvidia.com/gpu=all (только мониторинг)
           ▼                       ▼
   rootless podman (aleksey)     Tesla V100 32 GiB
     ├─ ocr         (rag_internal)
     ├─ embedding   (rag_internal)
     ├─ qwen38      (llm_network)
     └─ qwen-image  (qwen_image_gen_network + deploy_auth_network)
```

switchd **сам создаёт и удаляет** контейнеры управляемых сервисов (это единственный способ менять `-ngl`, `--ctx-size`,
CDI-устройства и лимиты без rebuild). Имена контейнеров сохраняются прежними (`ocr`, `embedding`, `qwen38`, `qwen-image`) —
DNS-имена внутри сетей не меняются, поэтому `rag-chat_chat_1` (`http://embedding:8091`), `rag-chat_processor_1`
(`http://ocr:8090`) и gateway (`http://qwen38:4001`, `http://qwen-image:8000`) продолжают работать без правок.

Из этого следует миграция: OCR и embedding уезжают из `docker-compose.yml` RAG-Chat (§9.1), quadlet `qwen-image`
удаляется (§9.2), startup-скрипты llama.cpp перестают использоваться (§9.3).

---

## 4. Ресурсная модель

### 4.1 Бюджеты

```yaml
host:
  ram_reserve_mib: 8192      # ОС + podman + незапланированное; не отдаём сервисам
  vram_reserve_mib: 1536     # драйвер + CUDA-контексты + округления nvidia-smi
  vram_device: 0
```

- `ram_budget = MemAvailable - ram_reserve`
- `vram_budget = total - used - vram_reserve`
- Клейм сервиса сравнивается с бюджетом **минус клеймы всех сервисов в состоянии `starting`/`ready`**, кроме самого целевого:
  `claim <= budget - Σ claims(other running|starting)`. То есть учёт ведётся по *заявкам*, а не по мгновенному `used`
  (иначе только что стартующий сервис, ещё не занявший VRAM, будет проигнорирован).
- VRAM считается по устройству (`nvidia-smi`), RAM — по `MemAvailable` (`/proc/meminfo`), с кэшем сэмпла 2 с.

### 4.2 Клеймы по профилям

Значения — стартовые (консервативные), после первого успешного старта **калибруются** (§4.3).

| Сервис | `off` | `cpu` | `gpu` |
|---|---|---|---|
| `ocr` | 0 | RAM 4096 MiB | VRAM 3072 MiB, RAM 1024 MiB |
| `embedding` | 0 | RAM 1024 MiB | VRAM 1536 MiB, RAM 512 MiB |
| `llm` | 0 | RAM 32768 MiB — **`available: false`** (27 GiB весов + KV не влезают под резерв 8 GiB; включать только `force`) | VRAM 30720 MiB (= 27.05 GiB веса + ctx 16k + CUDA ctx), RAM 2048 MiB |
| `image` | 0 | — **нет CPU-профиля** (`MEMORY_MODE=offload` — это всё равно GPU ~8 GiB) | VRAM 20480 MiB base + 2048 MiB на каждую референс-картинку, RAM 32768 MiB |

Правила:
- `supported_states` берётся из каталога; `unavailable_states[state].reason` — человеческая строка, её показывает UI.
- `force: true` в запросе разрешает старт вне бюджета **только** для `available:false` состояний и **никогда** не отключает
  проверку «бюджет < 0» по VRAM (иначе CUDA OOM). В аудит пишется `forced=true`.
- Для `image` клейм VRAM зависит от числа референсов — это runtime-параметр запроса
  (`POST /state {"state":"gpu","options":{"references":4}}`), по умолчанию 0. Клейм = `base + 2048*refs`.
  **Важно:** `image` в простое держит ~440 MiB VRAM (модель грузится лениво), но клейм всё равно 20 GiB — пик приходится
  на генерацию. Поэтому admission считается по клейму, а не по мгновенному `nvidia-smi`. У `image` в каталоге
  `calibrate_vram: false` (замер в простое занизил бы бюджет в 40 раз).
- Для `llm` в gpu-профиле `--ctx-size` фиксирован каталогом (`16384` в v1) — 128000 из старого скрипта физически не влезает
  рядом с 27 GiB весов. Если нужен больший ctx — уменьшать квант (Q4 ~15 GiB) или поднимать `vram_reserve` осознанно.

### 4.3 Калибровка (обязательная часть, не «nice to have»)

После каждого успешного старта switchd:

1. снимает `measured_ram` из cgroup контейнера (`podman stats --no-stream --format json`, поле `mem_usage`);
2. снимает `measured_vram` как **дельту** `nvidia-smi memory.used` до старта и после прогрева (+3 с), плюс кросс-проверка
   через `nvidia-smi --query-compute-apps=pid,used_memory` и маппинг pid → контейнер по `/proc/<pid>/cgroup`.
   Для сервисов с ленивой загрузкой модели (`calibrate_vram: false`, сейчас только `image`) VRAM не калибруется вовсе —
   остаётся клейм каталога;
3. пишет `measured_*` в БД и в аудит; клейм в каталоге не меняется (он источник истины для admission), но UI показывает оба числа;
4. если `measured > claim * 1.15` — событие `OVER_BUDGET` в лог и жёлтая пометка в UI («клейм занижен, откалибруйте»).

Отдельный эндпоинт `POST /api/services/{id}/recalibrate` запускает перезапуск вне очереди и обновляет замеры (для случая,
когда сменили модель/квант).

KV-кэш для планирования ctx считается из GGUF-метаданных, а не из головы:
`kv_bytes_per_token = 2 * n_layer * n_kv_heads * head_dim * 2` (K и V, f16), затем
`ctx_max = (vram_claim - weights - 512 MiB) / kv_bytes_per_token`. Ключи GGUF читаются из заголовка файла
(`*.block_count`, `*.attention.head_count_kv`, `*.attention.key_length`; для MoE/гибридов — с фолбэком на «неизвестно»,
тогда `ctx_max` не показываем). Это нужно, чтобы UI мог написать «LLM: 16384 ctx помещается, 32768 — нет» вместо угадывания.

### 4.4 Матрица совместимости (V100 32 GiB, резерв 1536 MiB)

| Комбинация | VRAM Σ | Влезает |
|---|---|---|
| `llm=gpu` + `ocr=off` + `emb=off` + `image=off` | 30720 | ✅ |
| `llm=gpu` + `ocr=gpu` + `emb=gpu` | 35328 | ❌ (не хватает ~2.5 GiB) |
| `image=gpu`(0 refs) + `ocr=gpu` + `emb=gpu` | 25088 | ✅ |
| `image=gpu`(4 refs) + `ocr=gpu` + `emb=gpu` | 33280 | ❌ |
| `image=gpu`(0 refs) + `llm=gpu` | 51200 | ❌ |
| `cpu`-режимы всех четырёх | 0 VRAM | ✅ (RAM: 4+1+32(force)+32 ≈ 69 GiB — только с `force` и без qwen-image) |

UI обязан показывать эту же арифметику в тексте отказа: `required_mib`, `available_mib`, `suggestions` (какие сервисы
выключить и сколько это освободит).

---

## 5. Состояния и переходы

### 5.1 Модель

```
       ┌─────────┐   start(profile)   ┌──────────┐  health ok   ┌───────────┐
       │   off   │───────────────────▶│ starting │─────────────▶│ ready:cpu │
       └─────────┘                    └────┬─────┘              │ ready:gpu │
            ▲                               │ fail/timeout    └─────┬─────┘
            │                               ▼                       │ stop
            │                          ┌────────┐                   ▼
            └──────────────────────────│ failed │◀────────────┌───────────┐
                                       └────────┘             │ stopping  │
                                                              └─────┬─────┘
                                                                    ▼
                                                                  off
```

- `desired_state` ∈ {`off`,`cpu`,`gpu`} — персистентно, задаётся API.
- `state` ∈ {`off`,`starting`,`ready`,`stopping`,`failed`} + `mode` ∈ {`cpu`,`gpu`} для `ready`.
- `failed` — терминальное для попытки; reconciler не ретраит автоматически (иначе цикл «старт-падает-старт»), ретрай только
  по явному запросу или после изменения каталога. `desired_state` при провале откатывается на прежнее значение, причина — в `transitions.error_*`.
- Пока идёт переход, сервис отвечает `423 BUSY` на новые запросы `POST /state` (кроме `force:true`, который возвращает 423 без изменений).

### 5.2 Алгоритм перехода (псевдокод, один на все пары состояний)

```
transition(svc, target, opts):
  lock(svc)                                   # один переход на сервис
  if svc.transition != nil: return 423
  plan = catalog.profile(svc, target)          # nil для off
  if plan == nil: return stop(svc)             # off
  if plan.available == false and not opts.force: return 422 STATE_NOT_SUPPORTED(reason)

  # 1) admission
  evicted = []
  for e in opts.evict or []:                   # явный список; UI присылает подсказку
     if catalog[e].preemptible == false and not opts.force:
        return 409 CANNOT_EVICT(e)             # LLM вытесняется только ADMIN'ом с force
  if not fits(plan, excluding=[svc] + evicted):
     if opts.evict and fits(plan, excluding=[svc]):
        evicted = stop_many(evicted)           # сначала освобождаем, потом стартуем
     else:
        return 409 INSUFFICIENT_VRAM|RAM(details, suggestions)

  reserve(svc, plan)                           # svc.state = starting; клейм учитывается сразу
  record(transition{from, to, actor, phase=admission})

  # 2) runtime
  try:
     ensure_container(plan)                    # если config_hash отличается — remove + create
     start(svc)
     wait_healthy(plan.health, plan.startup_timeout_s)   # 900s для OCR cold start
     measure_and_store(svc)                            # §4.3
     svc.state = ready; svc.mode = target
     commit(desired_state = target)
  except HealthTimeout: rollback(HEALTH_TIMEOUT)
  except ContainerExited: rollback(CONTAINER_EXITED, log_tail)
  except RuntimeError: rollback(RUNTIME_FAILED)
  finally: release(svc); audit(...)
```

`stop(svc)`:

1. `podman stop -t stop_grace_s` (по сервису: OCR 30, embedding 15, LLM 20, image 60 — image сохраняет outputs);
2. если через `stop_grace_s + 10` контейнер жив — `podman kill` + `podman rm -f`;
3. ждём, пока `nvidia-smi` покажет освобождение VRAM (до 30 с), иначе `VRAM_NOT_RELEASED` в лог (не блокирует);
4. `state=off`, контейнер **не удаляем** (быстрый повторный старт), но `config_hash` инвалидируется при смене профиля.

### 5.3 Rollback

Откат — это **не** «оставить как было», а явное восстановление:
- если до перехода сервис был `ready:gpu` и переход в `cpu` провалился — пробуем вернуть `gpu` профиль (admission заново);
- если вернуть нельзя (VRAM заняли) — `state=failed`, `desired_state=off`, в UI баннер с причиной и кнопкой `Retry`;
- контейнер после провала удаляется (`podman rm -f`), чтобы не остался «наполовину настроенный» инстанс.

### 5.4 Коды ошибок API

| HTTP | `error.code` | Когда |
|---|---|---|
| 400 | `BAD_STATE`, `BAD_OPTIONS` | неизвестное состояние, `references` вне 0..10 |
| 404 | `UNKNOWN_SERVICE` | нет такого `id` |
| 409 | `INSUFFICIENT_VRAM`, `INSUFFICIENT_RAM`, `CANNOT_EVICT`, `SERVICE_READONLY` | не влезает / нельзя вытеснить / сервис ещё управляется compose (фаза 1) |
| 422 | `STATE_NOT_SUPPORTED` | `cpu` для image, `cpu` для llm без `force` |
| 423 | `BUSY` | переход уже идёт |
| 502 | `HEALTH_TIMEOUT`, `CONTAINER_EXITED`, `VRAM_OOM` | рантайм не поднялся (тело содержит `log_tail`, ≤ 32 строк) |
| 503 | `RUNTIME_UNAVAILABLE` | нет podman-сокета / `nvidia-smi` (сервис жив, но управлять не может) |

Формат ошибки (как в Auth-Service API):

```json
{"error":{"code":"INSUFFICIENT_VRAM","message":"Image Gen needs 20480 MiB, 12320 MiB free after 1536 MiB reserve.",
"details":{"required_mib":20480,"available_mib":12320,"holders":[{"id":"llm","vram_mib":30720,"preemptible":false}],
"suggestions":[{"evict":["llm"],"frees_mib":30720}]}}}
```

---

## 6. API

База: `/api/services` (тот же путь и на хосте `filenkov.store` через gateway, и напрямую на `:8095`).
Все ответы `application/json`. Аутентификация — на gateway (`auth: admin`); switchd доверяет заголовкам `X-Auth-*`
и **не** публикует порт.

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/services` | состояние всех сервисов + бюджеты хоста (основной polling-эндпоинт) |
| `GET` | `/api/services/{id}` | один сервис |
| `POST` | `/api/services/{id}/state` | запрос перехода, **асинхронно**, `202` |
| `POST` | `/api/services/{id}/actions/restart` | перезапуск в текущем режиме |
| `POST` | `/api/services/{id}/actions/recalibrate` | перезапуск + обновление замеров |
| `GET` | `/api/services/{id}/logs?tail=200` | хвост логов контейнера (текст, `text/plain`) |
| `GET` | `/api/services/transitions?limit=50` | история переходов (для UI и разбора) |
| `GET` | `/healthz` | liveness, без зависимостей |
| `GET` | `/readyz` | `200` только если podman-сокет и `nvidia-smi` доступны |
| `GET` | `/metrics` | Prometheus, опционально |

### 6.1 `GET /api/services`

```json
{
  "host": {
    "ram": {"total_mib": 90112, "available_mib": 48128, "reserve_mib": 8192, "budget_mib": 39936, "claimed_mib": 33792},
    "vram": {"total_mib": 32768, "used_mib": 440, "reserve_mib": 1536, "budget_mib": 30792, "claimed_mib": 0},
    "cpu": {"threads": 16, "load1": 0.42},
    "device": {"index": 0, "name": "Tesla PG503-216", "compute_cap": "7.0"},
    "sampled_at": "2026-10-22T11:20:03Z"
  },
  "services": [
    {
      "id": "ocr",
      "title": "OCR (Surya OCR 2)",
      "kind": "ocr",
      "state": "ready",
      "mode": "cpu",
      "desired_state": "cpu",
      "supported_states": ["off", "cpu", "gpu"],
      "unavailable_states": {},
      "claim": {"ram_mib": 4096, "vram_mib": 3072},
      "measured": {"ram_mib": 1293, "vram_mib": 0, "at": "2026-10-22T10:02:11Z"},
      "container": {"name": "ocr", "id": "3f2a9c1b7d", "image": "localhost/rag-chat-ocr:cuda", "health": "healthy"},
      "health": {"ok": true, "checked_at": "2026-10-22T11:19:58Z", "detail": "GET http://ocr:8090/health → 200"},
      "consumers": ["rag-chat_processor_1"],
      "notes": ["weights 1.18 GiB + mmproj 0.19 GiB", "cold start до 900 s (первая загрузка весов)"]
    },
    {
      "id": "llm",
      "title": "LLM (Qwen 3.8)",
      "kind": "llm",
      "state": "off",
      "mode": null,
      "desired_state": "off",
      "supported_states": ["off", "gpu", "cpu"],
      "unavailable_states": {"cpu": {"reason": "27.05 GiB weights do not fit under the 8 GiB RAM reserve"}},
      "claim": {"ram_mib": 2048, "vram_mib": 30720},
      "measured": null,
      "container": {"name": "qwen38", "id": null, "image": "ghcr.io/ggml-org/llama.cpp:server-cuda12", "health": "absent"},
      "health": {"ok": false, "checked_at": "2026-10-22T11:19:58Z", "detail": "container not running"},
      "consumers": ["gateway (llm.filenkov.store)", "litellm-proxy-server (owl chat, сейчас Exited)"],
      "notes": ["gpu profile uses --ctx-size 16384; 128000 does not fit next to 27.05 GiB weights"]
    }
  ],
  "transition": null
}
```

Поля контракта обязательны; `measured`, `mode`, `container.id`, `transition` могут быть `null`.
`consumers` — статический список из каталога (кто упадёт, если сервис выключить), чтобы UI мог предупредить.

### 6.2 `POST /api/services/{id}/state`

```json
{"state": "gpu", "evict": ["image"], "force": false, "options": {"references": 2}}
```

- `state` обязателен: `off` | `cpu` | `gpu`.
- `evict` — список сервисов, которые разрешено остановить для освобождения места (порядок = порядок остановки).
- `options.references` — только для `image`.
- Идемпотентность: если `desired_state == state` и переход не идёт → `202` с `{"transition": null, "noop": true}`.

Ответ `202`:

```json
{"transition": {"id": "t_01JB7Q", "service_id": "image", "from": "off", "to": "gpu",
"phase": "admission", "started_at": "2026-10-22T11:21:00Z", "actor": "aleksey@example.com"}}
```

Переход **асинхронный**: браузер закрыл вкладку — переход всё равно доедет. Прогресс — в `GET /api/services`
(`transition.phase` ∈ `admission|evicting|pulling|creating|starting|warming|committing`).

Причина async: старт OCR cold — до 900 с, image — до 600 с; синхронный POST упирается в `timeouts.proxy` gateway (60 с)
и в таймауты браузера. SPA поллит `GET /api/services` каждые 1.5 с, пока есть незавершённый переход.

### 6.3 `GET /api/services/{id}/logs`

`text/plain`, последние `tail` строк (default 200, max 2000) из `podman logs`. Секреты маскируются: строки, содержащие
`api-key`, `Authorization: Bearer`, `HF_TOKEN`, заменяются на `…[redacted]…` (простое построчное правило, регэксп по имени
секрета из каталога).

### 6.4 Внутренние правила

- Один процесс, один мьютекс аллокатора, один переход на сервис (per-service lock), порядок захвата: `alloc → service`
  (иначе дедлок между «evict image для llm» и «evict llm для image»).
- Все внешние вызовы (`podman`, `nvidia-smi`) с таймаутом и контекстом запроса; `nvidia-smi` кэшируется 2 с singleflight.
- Никакой команды/пути из тела запроса не попадает в `podman run` — только `id` + объявленный профиль (§10).

---

## 7. Runtime-слой (rootless podman)

### 7.1 Доступ к podman

switchd — rootless-контейнер под пользователем `aleksey` (quadlet, §7.4) с примонтированным сокетом:

```ini
Volume=/run/user/1000/podman/podman.sock:/run/podman/podman.sock:rw
Environment=CONTAINER_HOST=unix:///run/podman/podman.sock
AddDevice=nvidia.com/gpu=all
SecurityLabelDisable=true
```

Требуется `systemctl --user enable --now podman.socket` (сейчас `podman.socket` не активен; `Linger=yes` уже стоит).
Клиент — `github.com/containers/podman/v5/pkg/bindings` либо прямой HTTP по unix-сокету; CLI не вызываем
(нужен стабильный машинный вывод, а не парсинг человекочитаемого).

Альтернатива, если контейнер с сокетом окажется неудобен: switchd как бинарь в `systemctl --user`, gateway ходит на
`host.docker.internal:8095` (у gateway уже есть `extra_hosts: host.docker.internal:host-gateway`), bind на адрес
podman-бриджа, не на `0.0.0.0`. Решение по умолчанию — контейнер: он попадает в `deploy_auth_network` под DNS `switch`,
и не надо светить порт на хосте.

### 7.2 Операции

| Операция | Реализация |
|---|---|
| `ensure_container(plan)` | `GET /containers/{name}/json`; если `config_hash` (sha256 от image+cmd+env+devices+mounts+net) совпал — ничего; иначе `POST /containers/{name}/stop`, `DELETE`, `POST /containers/create` |
| `start` | `POST /containers/{name}/start` |
| `stop` | `POST /containers/{name}/stop?t=<grace>`; при таймауте `POST /kill` + `DELETE?force=true` |
| `wait_healthy` | HTTP-проба (см. 7.3) каждые 2 с до `startup_timeout_s`; параллельно следим, что контейнер не `exited` (иначе `CONTAINER_EXITED` + `logs?tail=50`) |
| `measure` | `podman stats --no-stream` (cgroup) + `nvidia-smi` (§4.3) |
| `inspect` | состояние, `StartedAt`, `ExitCode`, `OOMKilled` |

Конфигурация контейнера при создании:

- `HostConfig.RestartPolicy = {"Name": "no"}` — **важно**: политику восстановления держит switchd, а не podman.
  Иначе остановленный сервис будет подниматься сам (как сейчас quadlet с `Restart=always`) и «off» станет недостижим.
- `HostConfig.DeviceRequests` не используем — только CDI (`HostConfig.Devices = ["nvidia.com/gpu=all"]`), потому что
  `deploy.resources` на этом podman не работает (проверено в RAG-Chat, см. комментарий в `docker-compose.gpu.yml`).
- `HostConfig.Memory` = `claim.ram_mib` + 2048 MiB запас (жёсткий лимит, чтобы один сервис не съел хост);
  для image — `64g` как сейчас; `HostConfig.ShmSize` для image — `8g`.
- `HostConfig.NetworkMode` + `EndpointsConfig` — сети из каталога (иначе DNS-имена пропадут).
- `User` — как в образе (10001), `SecurityOpt` — без изменений.
- `Env` — из `config/secrets.env` (0600) + значения профиля; **в API и логи не отдаются**.

### 7.3 Health-пробы

switchd подключается к `deploy_auth_network`, `rag_internal`, `llm_network`, `qwen_image_gen_network` — тогда пробы идут
по DNS-именам, как у потребителей:

| Сервис | Проба | Трактовка |
|---|---|---|
| `ocr` | `GET http://ocr:8090/health` | `200` только когда модель загружена и llama-server отвечает (`app.py` → `GET /health`) |
| `embedding` | `GET http://embedding:8091/health` | `200` при живом encoder, `503` пока модель грузится |
| `llm` | `GET http://qwen38:4001/health` | `{"status":"ok"}` |
| `image` | `GET http://qwen-image:8000/healthz` | `200` (ленивая загрузка модели допускается — тогда в UI `ready` + `warming`, см. ниже) |

Сети должны существовать на момент старта switchd (их создают стеки). Если сети нет — switchd стартует, но сервис,
привязанный к отсутствующей сети, помечается `failed` с `RUNTIME_UNAVAILABLE` и повторяется в reconciler каждые 60 с.
Fallback-проба без сети: `podman exec <ctr> python -c "…urllib…"` (есть в OCR/embedding/image; для llama.cpp-образа — `curl`,
если он в образе, иначе сеть обязательна).

`image` после `int8`-прогрева отдаёт `200` до готовности модели (модель грузится в фоне; README: «background model preload»).
Поэтому для него `ready` = контейнер жив + `/healthz` 200, а в `notes` UI пишет «модель догружается, первая генерация дольше».

### 7.4 systemd и boot

`~/.config/containers/systemd/switchd.container` (rootless quadlet, `WantedBy=default.target`, `Restart=always`).
Порядок загрузки:

1. `podman.socket` (user) — `WantedBy=sockets.target`;
2. `switchd` стартует, читает desired state из SQLite;
3. reconciler поднимает то, что должно быть поднято (`starting` → `ready`), но **не ретраит** `failed`;
4. `podman-restart.service` (user) не нужен: политика `restart=no` + reconciler покрывает boot. Включать его нельзя —
   он поднимет контейнеры, которые должны остаться выключенными.

---

## 8. Каталог сервисов: `config/services.yaml`

Полный пример (значения — стартовые, калибруются):

```yaml
host:
  ram_reserve_mib: 8192
  vram_reserve_mib: 1536
  vram_device: 0
  reconcile_interval_s: 15
  nvidia_smi_cache_s: 2

defaults:
  stop_grace_s: 30
  startup_timeout_s: 300
  health_interval_s: 2
  restart_policy: "no"

services:
  - id: ocr
    title: "OCR (Surya OCR 2)"
    kind: ocr
    container_name: ocr
    health: {url: "http://ocr:8090/health", timeout_s: 5}
    consumers: ["rag-chat_processor_1"]
    preemptible: true
    notes: ["weights 1.18 GiB + mmproj 0.19 GiB", "cold start до 900 s"]
    profiles:
      cpu:
        image: localhost/rag-chat-ocr:cuda
        networks: [rag_internal]
        volumes: ["rag-chat_ocr-model-cache:/var/cache/huggingface"]
        env: {OCR_TIMEOUT: "120", HF_TOKEN: "${HF_TOKEN}"}
        cmd: ["--ngl", "0"]
        claim: {ram_mib: 4096, vram_mib: 0}
        startup_timeout_s: 900
      gpu:
        image: localhost/rag-chat-ocr:cuda
        networks: [rag_internal]
        devices: ["nvidia.com/gpu=all"]
        volumes: ["rag-chat_ocr-model-cache:/var/cache/huggingface"]
        env: {OCR_TIMEOUT: "120", HF_TOKEN: "${HF_TOKEN}"}
        cmd: ["--ngl", "999"]
        claim: {ram_mib: 1024, vram_mib: 3072}
        startup_timeout_s: 900

  - id: embedding
    title: "Embedder (EmbeddingGemma 2)"
    kind: embedding
    container_name: embedding
    health: {url: "http://embedding:8091/health", timeout_s: 5}
    consumers: ["rag-chat_chat_1", "rag-chat_processor_1"]
    preemptible: true
    profiles:
      cpu:
        image: localhost/rag-chat_embedding:latest
        networks: [rag_internal]
        env: {EMBEDDING_LLAMA_NGL: "0"}
        claim: {ram_mib: 1024, vram_mib: 0}
      gpu:
        image: localhost/rag-chat_embedding:latest
        networks: [rag_internal]
        devices: ["nvidia.com/gpu=all"]
        env: {EMBEDDING_LLAMA_NGL: "999"}
        claim: {ram_mib: 512, vram_mib: 1536}

  - id: llm
    title: "LLM (Qwen 3.8)"
    kind: llm
    container_name: qwen38
    health: {url: "http://qwen38:4001/health", timeout_s: 5}
    consumers: ["gateway (llm.filenkov.store)"]
    preemptible: false
    notes: ["gpu: ctx 16384; 128000 не влезает рядом с 27.05 GiB весов"]
    profiles:
      cpu:
        available: false
        reason: "27.05 GiB weights do not fit under the 8 GiB RAM reserve"
        image: ghcr.io/ggml-org/llama.cpp:server-cuda12
        networks: [llm_network]
        volumes: ["/media/aleksey/HardDisk/gguf_models:/models:ro"]
        cmd: ["--model", "/models/HuiHui/qwen3.8/model_q8.gguf", "--ctx-size", "16384",
              "--host", "0.0.0.0", "--port", "4001", "--api-key", "${LLM_API_KEY}",
              "--reasoning", "off", "-ngl", "0"]
        claim: {ram_mib: 32768, vram_mib: 0}
        startup_timeout_s: 600
      gpu:
        image: ghcr.io/ggml-org/llama.cpp:server-cuda12
        networks: [llm_network]
        ports: ["4001:4001"]
        devices: ["nvidia.com/gpu=all"]
        volumes: ["/media/aleksey/HardDisk/gguf_models:/models:ro"]
        cmd: ["--model", "/models/HuiHui/qwen3.8/model_q8.gguf", "--ctx-size", "16384",
              "--host", "0.0.0.0", "--port", "4001", "--api-key", "${LLM_API_KEY}",
              "--reasoning", "off", "--flash-attn", "on", "-ngl", "-1"]
        claim: {ram_mib: 2048, vram_mib: 30720}
        startup_timeout_s: 600

  - id: image
    title: "Image Gen (Qwen-Image 2.1)"
    kind: image
    container_name: qwen-image
    health: {url: "http://qwen-image:8000/healthz", timeout_s: 10}
    consumers: ["gateway (image.filenkov.store)", "WebStorage POST /api/generated"]
    preemptible: true
    notes: ["int8: ~20 GiB VRAM + ~2 GiB KV на референс", "RAM 33 GiB (fp16-хвост энкодера)"]
    profiles:
      gpu:
        image: localhost/qwen-image-service:latest
        networks: [qwen_image_gen_network, deploy_auth_network]
        devices: ["nvidia.com/gpu=all"]
        volumes: ["qwen-hf-cache:/data/huggingface", "qwen-outputs:/data/outputs"]
        shm_size: "8g"
        memory_limit_mib: 65536
        env_file: "/home/aleksey/projects/qwen-image-service/.env"
        claim: {ram_mib: 32768, vram_mib: 20480}
        vram_per_reference_mib: 2048
        calibrate_vram: false     # модель грузится лениво: пик только на генерации
        startup_timeout_s: 600
        stop_grace_s: 60
```

Правила каталога:
- Профиля `off` нет по определению (нет контейнера).
- `available: false` + `reason` — состояние показывается в UI как недоступное, с причиной в `title`/`aria-describedby`.
- `${VAR}` подставляется из `config/secrets.env` (0600); в JSON-ответах и логах значения не появляются.
- Изменение каталога = `SIGHUP` (перечитать) или рестарт; при несовпадении `config_hash` у запущенного контейнера
  switchd показывает `drift` в UI и предлагает `restart` (сам не перезапускает — это может быть во время генерации).
- `preemptible: false` у LLM означает: автоматически вытеснять его нельзя, только явным `evict` + `force:true` от ADMIN
  (UI спрашивает подтверждение вторым кликом).
- `readonly: true` — сервис ещё не передан switchd (например, `ocr`/`embedding` в фазе 1): состояние читается из podman,
  переключение → `409 SERVICE_READONLY`.

---

## 9. Что меняется в других репозиториях

### 9.1 RAG-Chat

1. `docker-compose.yml`: сервисы `ocr` и `embedding` убрать из стека (перенести под `profiles: ["standalone"]`,
   чтобы `podman compose up` их не поднимал) — иначе compose и switchd будут драться за одни и те же имена.
   Остальные сервисы (`processor`, `chat`) не меняются: они ходят на `http://ocr:8090` и `http://embedding:8091`,
   а switchd создаёт контейнеры с теми же именами в сети `rag_internal`.
2. `services/embedding/embedding/loader.py`: `build_llama_server_command` должен передавать `-ngl` из окружения
   (`EMBEDDING_LLAMA_NGL`, default `0` для cpu-профиля). Сейчас `-ngl` не передаётся вообще, и режим зависит от
   дефолта llama.cpp — это надо зафиксировать явно, иначе «cpu» может молча оказаться «gpu».
3. `services/ocr`: `build_llama_server_command` уже передаёт `-ngl 999` жёстко. Заменить на переменную
   `OCR_LLAMA_NGL` (default `999`), чтобы cpu-профиль запускался из того же CUDA-образа с `-ngl 0`.
   Тогда `docker-compose.gpu.yml` и `--target cuda` для переключения больше не нужны (остаются для сборки образа).
4. Один образ вместо двух: `--target cuda` собирается всегда (он же умеет cpu через `-ngl 0`), `--target cpu` остаётся
   только как «тонкий» вариант для машин без CUDA. В каталоге switchd по умолчанию `localhost/rag-chat-ocr:cuda`
   (CUDA-библиотеки в образе уже есть; на CPU-режим это +~150 MiB RAM на загруженные libcuda, приемлемо).
5. `Makefile`: `up-gpu` больше не способ переключения — остаётся только сборка. В README — ссылка на этот документ.

### 9.2 qwen-image-service

1. Удалить `~/.config/containers/systemd/qwen-image.container` (сейчас `Restart=always` — с ним «off» невозможен:
   systemd мгновенно перезапустит остановленный контейнер). Файл в репозитории (`deploy/qwen-image.container`) оставить
   как документацию «как выглядит контейнер», но в `~/.config/containers/systemd/` его быть не должно.
2. `podman rm -f qwen-image` перед первым старт-переходом (иначе switchd увидит чужой `config_hash` и пересоздаст —
   это нормально, но лучше сделать явно и один раз).
3. `.env` (в нём `MEMORY_MODE`, `DEVICE`, `PRELOAD_MODEL`) читается switchd как `env_file` профиля — путь абсолютный,
   потому что switchd работает из контейнера и должен видеть файл (примонтировать `/home/aleksey/projects/qwen-image-service/.env:ro`).
4. Если нужен CPU-режим для image — это `MEMORY_MODE=offload`, но он всё равно требует ~8 GiB VRAM, поэтому в каталоге
   профиля `cpu` нет. Если когда-нибудь понадобится полностью без GPU — это отдельная задача (Flask + torch CPU), вне v1.

### 9.3 llama.cpp

`startup_qwen3.8_*.sh`, `startup_qwen3.6_images.sh` — удалить или пометить `DEPRECATED: managed by ServiceSwitch`
(они дублируют каталог и содержат захардкоженный `--api-key`; ключ переезжает в `config/secrets.env`).
`--ctx-size 128000` из скрипта не переносится: он не влезает в 32 GiB (см. §1.4).

---

## 10. Интеграция с Auth-Service

### 10.1 Gateway (`deploy/gateway.yaml`)

Добавить upstream:

```yaml
upstreams:
  # ServiceSwitch (rootless-контейнер switchd на deploy_auth_network).
  switch: "http://switch:8095"
```

И маршруты на хосте `filenkov.store` — **строго перед** `- match: /api/*` (first match wins, иначе `/api/services` уедет
в Auth API под `auth: session`):

```yaml
      - match: /api/services
        upstream: switch
        auth: admin             # hub global_role == ADMIN
        preserve_host: false
      - match: /api/services/*
        upstream: switch
        auth: admin
        preserve_host: false
        # переходы асинхронные, поэтому 60 с дефолта достаточно;
        # долгие операции живут в фоне switchd, а не в этом запросе
```

Порядок вставки (текущий файл): после `- match: /api/logout` и **до** `- match: /api/me` / `- match: /api/*`.
Тесты gateway: `TestLoadDefaultYAML` (проверить `MatchMethod`/`auth: admin` для `/api/services`), `TestRouterFirstMatch`
(`/api/services` не попадает в `api`), `TestHubServicesAdminOnly` (аноним → 401, `FAMILY` → 403, `ADMIN` → 200 с проксированием
и заголовками `X-Auth-*`).

Не добавлять новый host `switch.filenkov.store` в v1: UI живёт в существующей админке, отдельный хост — лишняя
auth-поверхность. (Если понадобится прямой доступ — добавить host с `role_field: global_role`, catch-all `auth: admin`.)

### 10.2 SPA (`web/src/`)

**`types.ts`**:

```ts
export type ServiceState = 'off' | 'cpu' | 'gpu'
export type ServiceRuntimeState = 'off' | 'starting' | 'ready' | 'stopping' | 'failed'

export type ManagedService = {
  id: string
  title: string
  kind: 'ocr' | 'embedding' | 'llm' | 'image'
  state: ServiceRuntimeState
  mode: 'cpu' | 'gpu' | null
  desired_state: ServiceState
  supported_states: ServiceState[]
  unavailable_states: Record<string, { reason: string }>
  claim: { ram_mib: number; vram_mib: number }
  measured: { ram_mib: number; vram_mib: number; at: string } | null
  container: { name: string; id: string | null; image: string; health: string }
  health: { ok: boolean; checked_at: string; detail: string }
  consumers: string[]
  notes: string[]
}
export type HostBudget = {
  ram: { total_mib: number; available_mib: number; reserve_mib: number; budget_mib: number; claimed_mib: number }
  vram: { total_mib: number; used_mib: number; reserve_mib: number; budget_mib: number; claimed_mib: number }
  cpu: { threads: number; load1: number }
  device: { index: number; name: string; compute_cap: string }
  sampled_at: string
}
export type ServicesSnapshot = { host: HostBudget; services: ManagedService[]; transition: Transition | null }
```

**`api.ts`**: `fetchServices(): Promise<ServicesSnapshot>`, `putServiceState(id, state, opts?)` (`POST`,
`{state, evict?, force?, options?}`), `fetchServiceLogs(id, tail)`. Всё через существующий `request()` (credentials,
401 → `/api/refresh` → retry) — новой авторизации не появляется.

**`pages/Admin.tsx`**: вторая секция на той же странице, ниже Directory:

- заголовок: `<section className="a-section"><h2>Services</h2>` + предложение
  «Switch a service between off, CPU and GPU. Free memory is checked before anything starts.»
- бюджет: `<ul className="a-metrics">` с двумя `a-metric` — `RAM 47.2 / 88 GiB budget`, `VRAM 1.4 / 32 GiB`;
  третья строка (`a-metric-how`) — «MemAvailable minus 8 GiB reserve · nvidia-smi minus 1.5 GiB reserve», это и есть
  объяснение, откуда цифры.
- таблица `.table-wrap`: колонки `Service`, `State`, `RAM`, `VRAM`, `Health`, `Updated`.
  - `State` — три кнопки `.btn` (Off / CPU / GPU), активная с `aria-pressed="true"`, недоступная — `disabled` +
    `title` с причиной из `unavailable_states`; во время перехода — disabled у всех трёх + текст фазы
    (`Starting…`, `Stopping…`, `Warming model…`) в `aria-live="polite"`.
  - `RAM`/`VRAM` — `measured` (факт) и `claim` в скобках, например `1.26 GB / claim 4 GiB`.
  - `Health` — `OK` или `FAILED` с `status-blocked`-подобной окраской; `detail` в `title`.
- ошибка перехода — `.banner.error` с текстом из `error.message` + строкой-подсказкой из `details.suggestions`
  («Stop Image Gen to free 20 GiB»); кнопка `Retry` не нужна — достаточно повторного клика по состоянию.
- подтверждение для `preemptible: false`: первый клик превращает кнопку в `Stop LLM and start Image Gen?` (inline, без модала),
  второй клик отправляет `evict: ["llm"]`.
- выключенный сервис с живыми потребителями — строка `.a-callout` под таблицей: «OCR is off. Uploading PDFs in RAG-Chat
  will fail until it is on.»
- пустое состояние: `.empty` — `No managed services.`; ошибка загрузки: `.error-block` с кнопкой `Reload`.
- поллинг: `GET /api/services` каждые 5 с; 1.5 с, пока `transition != null`; пауза при `document.hidden`.

Стили — только существующие классы из `docs/service-design-language.md` (`.a-section`, `.a-metrics`, `.table-wrap`,
`.banner.error`, `.btn`, `.empty`, `.a-callout`, `.status-blocked`). Новых токенов/цветов не вводить.

**Тесты** (`web/src/pages/Admin.test.tsx`, `api.test.ts`): рендер трёх состояний; disabled для `unavailable_states`;
`409` → баннер с `required/available`; успешный `POST` → `202` → локальный `transition` → после поллинга `ready:gpu`;
`evict`-подтверждение для LLM; поллинг останавливается при `document.hidden`; `api.test.ts` — `fetchServices`
(`credentials: 'include'`, 401 → refresh → retry), `putServiceState` тело запроса.

### 10.3 Роли

Новых ролей нет: `auth: admin` = `global_role == ADMIN`, как `/admin` и `GET /users`. Аудит пишет email из `X-Auth-Email`
и `X-Auth-User-Id`. Сервисный доступ для CI — §14.3 (статический токен, отдельный заголовок, только на чтение+state).

---

## 11. Безопасность

- switchd **не публикует порт** (`ports:` отсутствует), доступен только из `deploy_auth_network` через gateway.
- Сокет podman = права пользователя `aleksey` на его контейнеры. Поэтому: образ switchd собирается из этого репозитория,
  пиннится по digest, `Pull=never` для локальной сборки; никаких `podman run` из пользовательского ввода.
- API принимает только `id` из каталога и `state` из перечисления. Ни `cmd`, ни `image`, ни `env` из тела запроса.
- `evict` ограничен списком `preemptible: true` (LLM — только с `force`-подтверждением от ADMIN, аудит `forced=true`).
- Gateway уже срезает входящие `X-Auth-*`, `X-User-Id`, `X-Storage-Role`; switchd **никогда** не доверяет этим заголовкам
  снаружи (только от gateway; bind в контейнере — `:8095` в bridge-сеть, не на хост).
- Секреты: `config/secrets.env` (0600, `HF_TOKEN`, `LLM_API_KEY`) монтируется в контейнер `:ro`; маскируются в логах
  и никогда не попадают в JSON. `--api-key` LLM не печатается в `GET /api/services`.
- Rate limit — на gateway (180 rpm); switchd дополнительно ограничивает 1 переход на сервис и 4 глобально.
- Аудит неизменяем (append-only таблица, без UPDATE/DELETE из API).

---

## 12. Наблюдаемость и NFR

| Метрика | Цель |
|---|---|
| `GET /api/services` p95 | < 50 мс (сэмпл `nvidia-smi` кэшируется 2 с, `podman stats` — 5 с) |
| Точность бюджета | ошибка ≤ 5% против `nvidia-smi`/`MemAvailable` сразу после старта |
| Reconciler | каждые 15 с; дрейф → запись в `transitions` (`drift`), не более 1 попытки в минуту на сервис |
| Время переключения | `off→cpu` OCR ≤ 60 с (тёплый кэш), `off→gpu` LLM ≤ 120 с, `off→gpu` image ≤ 180 с (модель в кэше); cold — по `startup_timeout_s` |
| Логи | JSON в journald (`systemctl --user -u switchd`), поля `service`, `transition_id`, `actor`, `phase`, `error_code` |
| `/metrics` | `switchd_service_state{id,mode}`, `switchd_measured_ram_mib{id}`, `switchd_measured_vram_mib{id}`, `switchd_transitions_total{id,to,result}` |
| Отказоустойчивость | падение switchd не трогает контейнеры; после рестарта — сверка desired/actual и продолжение незавершённого перехода (фаза `warming` переигрывается) |

---

## 13. План внедрения

| Фаза | Содержание | Готово, когда |
|---|---|---|
| **0. Калибровка** | switchd умеет `podman run` из каталога и меряет RAM/VRAM; прогон по 4 сервисам в одиночку (оба режима, где есть) | в БД реальные `measured_*`, матрица §4.4 подтверждена или исправлена |
| **1. Рантайм** | create/start/stop/health/rollback, SQLite, reconciler; управление `qwen38` и `qwen-image` (quadlet удалён) | `POST /state` переключает LLM и Image Gen; `off` реально держится после ребута |
| **2. RAG-Chat** | `-ngl` в embedding/ocr, `ocr`/`embedding` уезжают из compose, DNS сохранён | `podman compose up` не поднимает ocr/embedding; processor/chat работают; `make up-gpu` не нужен |
| **3. Auth** | gateway-маршрут + секция Services в `/admin` + тесты | ADMIN видит RAM/VRAM, переключает, получает внятный 409; аноним — 401, FAMILY — 403 |
| **4. Эксплуатация** | аудит, `/metrics`, `logs`-эндпоинт, README, runbook | переключение LLM↔Image Gen делается в 2 клика без ssh |

### Acceptance criteria (проверяемые)

1. `GET /api/services` отдаёт 4 сервиса, бюджеты RAM/VRAM и `measured` для запущенных; аноним → 401, `FAMILY` → 403.
2. `POST /api/services/llm/state {"state":"gpu"}` при работающем `image=gpu` → `409 INSUFFICIENT_VRAM` с `required_mib`,
   `available_mib`, `suggestions`; контейнеры **не** тронуты (`podman ps` до/после совпадает).
3. Тот же запрос с `{"evict":["image"]}` → `202`, `image` останавливается, VRAM освобождается, `qwen38` поднимается и
   проходит `/health`; `GET /api/services` в итоге `{llm: ready:gpu, image: off}`.
4. `POST /api/services/image/state {"state":"cpu"}` → `422 STATE_NOT_SUPPORTED` с причиной.
5. `POST /api/services/llm/state {"state":"cpu"}` без `force` → `422` с причиной «27.05 GiB …»; с `force:true` →
   либо старт, либо `409 INSUFFICIENT_RAM` (в зависимости от фактического `MemAvailable`), в аудите `forced=true`.
6. `docker stop`/`podman stop` контейнера вручную → в течение 15 с reconciler возвращает заявленное состояние (или `failed` с причиной).
7. Ребут хоста: сервисы, которые были `gpu`/`cpu`, поднимаются сами; те, что `off`, остаются `off`.
8. Убить switchd во время `starting` → после рестарта переход продолжается (или помечается `failed` с логом), контейнер не остаётся «ни жив ни мёртв».
9. `POST /state` для уже целевого состояния → `202 {"noop":true}` без действий.
10. В логах и JSON нет `LLM_API_KEY`, `HF_TOKEN`; `GET /logs` их не показывает.

По фазе 1: `ocr` и `embedding` в каталоге присутствуют, но помечены `readonly: true` — состояние читается из podman,
переключение отключено (`409 SERVICE_READONLY`), пока идёт фаза 2. Иначе UI будет обещать переключение, которого
ещё нет.

---

## 14. Если Jenkins всё-таки нужен

### 14.1 Роль

Только **триггер** и/или **CI сборки образов**. Никакого доступа к podman-сокету, `systemctl`, файлам каталога.
Jenkins в этой роли не может нарушить инвариант «единственный писатель — switchd».

### 14.2 Стоимость, которую надо принять

JVM + плагины ≈ 1 GiB RAM постоянно (на хосте, где `qwen-image` держит 33 GiB и свободно ~47 GiB — терпимо, но бессмысленно),
плюс обновления, плюс вторая auth-поверхность (Jenkins сам умеет логины и легко становится дырой, если его выставить наружу).
Поэтому если Jenkins уже есть в инфраструктуре — ок; заводить его *ради* этой задачи — нет.

### 14.3 Доступ из CI

`switchd` поддерживает второй способ аутентификации — статический токен для машин:

- заголовок `X-Service-Token: <token>` (значение из `config/secrets.env`, `SWITCHD_SERVICE_TOKEN`, ≥32 байта);
- разрешены только `GET /api/services*` и `POST /api/services/{id}/state`; `evict` для `preemptible:false` — запрещён
  (403 `FORBIDDEN_FOR_TOKEN`), чтобы CI не мог выключить LLM;
- токен не принимается, если запрос пришёл через gateway (там уже `auth: admin`); сравнение — `subtle.ConstantTimeCompare`;
- всё, что сделал токен, пишется в аудит с `actor = "service:<name>"`.

### 14.4 Пример `Jenkinsfile` (тонкий клиент)

```groovy
pipeline {
  agent any
  options { timeout(time: 5, unit: 'MINUTES') }
  parameters {
    choice(name: 'SERVICE', choices: ['ocr', 'embedding', 'llm', 'image'], description: 'Managed service')
    choice(name: 'STATE',   choices: ['gpu', 'cpu', 'off'],                        description: 'Target state')
  }
  environment {
    SWITCH = 'http://switch:8095/api/services'
    TOKEN  = credentials('switchd-service-token')
  }
  stages {
    stage('Request transition') {
      steps {
        sh '''
          curl -fsS -X POST "$SWITCH/$SERVICE/state" \
            -H "X-Service-Token: $TOKEN" -H 'Content-Type: application/json' \
            -d "{\\"state\\":\\"$STATE\\"}" | tee /tmp/switch.json
        '''
      }
    }
    stage('Wait for convergence') {
      steps {
        sh '''
          for i in $(seq 1 120); do
            body=$(curl -fsS "$SWITCH" -H "X-Service-Token: $TOKEN")
            echo "$body" | jq -e --arg s "$SERVICE" --arg t "$STATE" \
              '.services[] | select(.id==$s) | .desired_state==$t and .transition==null' >/dev/null && exit 0
            sleep 2
          done
          echo "did not converge"; exit 1
        '''
      }
    }
  }
  post { failure { sh 'curl -fsS "$SWITCH/$SERVICE/logs?tail=100" -H "X-Service-Token: $TOKEN" || true' } }
}
```

Jenkins-агент должен стоять в `deploy_auth_network` (DNS `switch`) или ходить через gateway с admin-cookie — первое проще.

---

## 15. Риски и подводные камни

| # | Риск | Что делать |
|---|---|---|
| 1 | **V100 32 GiB не вмещает LLM+Image** — требование «оба на GPU» невыполнимо | честный 409 + `evict`; в UI матрица §4.4; при желании — Q4-квант LLM (~15 GiB), тогда пара влезает |
| 2 | `qwen-image` держит **33 GiB RAM** и лимит `Memory=64g` больше свободной RAM | admission по RAM обязателен; лимит в каталоге — `65536` оставляем, но клейм 32 GiB |
| 3 | quadlet с `Restart=always` не даёт выключить image | удалить юнит из `~/.config/containers/systemd/`, `daemon-reload` (§9.2) |
| 4 | compose RAG-Chat перезапустит ocr/embedding после `podman compose up` | убрать их из стека (§9.1); иначе два источника истины |
| 5 | `deploy.resources`/`DeviceRequests` на этом podman **не работает** | только CDI `nvidia.com/gpu=all` (уже проверено в `docker-compose.gpu.yml`) |
| 6 | CUDA-контекст съедает ~300–500 MiB на процесс, `nvidia-smi` округляет | `vram_reserve_mib: 1536`; калибровка меряет дельту, а не «вес файла» |
| 7 | OCR cold start качает 1.4 GiB весов, `startup_timeout` 900 с; прокси-таймаут 60 с | async-переходы + поллинг; никогда не делать синхронный POST |
| 8 | `-ngl` в embedding сейчас не задан — «cpu» может оказаться «gpu» | патч `loader.py` (§9.1.2), в каталоге env `EMBEDDING_LLAMA_NGL` |
| 9 | `--ctx-size 128000` у LLM не влезает в 32 GiB | gpu-профиль `16384`; расчёт `ctx_max` из GGUF-метаданных (§4.3) |
| 10 | rootless-сокет не активен (`podman.socket` inactive) | `systemctl --user enable --now podman.socket`; проверить `Linger=yes` |
| 11 | switchd в контейнере не видит `.env` qwen-image и GGUF-диск | монтировать `/media/aleksey/HardDisk/gguf_models:ro` и `qwen-image-service/.env:ro` в switchd, путь в каталоге — абсолютный |
| 12 | сеть `qwen_image_gen_network` может быть пересоздана стеком | switchd переподключается при `RUNTIME_UNAVAILABLE`; health-проба через `podman exec` как fallback |
| 13 | `litellm-proxy-server` сейчас `Exited` — owl chat и `/api/chat` мертвы независимо от switchd | вне scope, но в UI `consumers` LLM это видно; отдельная задача |
| 14 | `--cache-ram -1` у OCR (кэш промптов в RAM без лимита) | клейм RAM 4 GiB и `Memory` лимит 6 GiB; при `OVER_BUDGET` — снижать `--cache-ram` |
| 15 | одновременные `POST` из UI и Jenkins | per-service lock + 423; глобальный мьютекс аллокатора; идемпотентный `noop` |

---

## 16. Открытые вопросы (нужно решение до фазы 2)

1. **ctx для LLM на GPU**: 16384 (влезает рядом с OCR/embedder) или 32768 (тогда OCR/embedder только `cpu`)?
2. **Q4-квант LLM** вместо Q8 (~15 GiB): открывает совместный режим LLM+Image? Если да — добавить профиль `gpu-small`.
3. **Image Gen CPU**: нужен ли вообще (сейчас нет профиля)? Если да — это отдельный образ на torch-CPU, вне v1.
4. **Автовыключение простоя**: гасить image/llm через N минут без запросов? (сейчас не в scope, но reconciler это умеет).
5. **Кто владеет сборкой образов**: остаётся `make`/`podman build` руками, или заводим GitHub Actions?
6. **`switch.filenkov.store`** как отдельный хост — нужен ли, или хватает секции в `/admin`?

---

## Приложение A. Схема БД (SQLite, `modernc.org/sqlite`, без cgo)

```sql
CREATE TABLE IF NOT EXISTS services (
  id             TEXT PRIMARY KEY,
  desired_state  TEXT NOT NULL CHECK (desired_state IN ('off','cpu','gpu')),
  state          TEXT NOT NULL CHECK (state IN ('off','starting','ready','stopping','failed')),
  mode           TEXT CHECK (mode IN ('cpu','gpu')),
  config_hash    TEXT,
  measured_ram_mib  INTEGER,
  measured_vram_mib INTEGER,
  measured_at    TEXT,
  last_error     TEXT,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transitions (
  id           TEXT PRIMARY KEY,
  service_id   TEXT NOT NULL REFERENCES services(id),
  from_state   TEXT,
  to_state     TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('running','ok','failed','drift')),
  phase        TEXT,
  error_code   TEXT,
  error_detail TEXT,
  forced       INTEGER NOT NULL DEFAULT 0,
  actor        TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS transitions_service_time ON transitions(service_id, started_at DESC);

CREATE TABLE IF NOT EXISTS audit (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  at         TEXT NOT NULL,
  actor      TEXT NOT NULL,
  action     TEXT NOT NULL,
  service_id TEXT,
  detail     TEXT
);
```

## Приложение B. Структура Go-модуля

```
Switch-Service/
  cmd/switchd/main.go          # флаги, конфиг, сигналы (SIGHUP = reload каталога)
  internal/catalog/            # YAML, валидация, config_hash, ${VAR} из secrets.env
  internal/alloc/              # бюджеты, admission, резервирование, мьютекс
  internal/state/              # SQLite: services, transitions, audit
  internal/runtime/podman/     # bindings-клиент, create/start/stop/inspect/logs/stats
  internal/runtime/nvidia/     # nvidia-smi сэмплер (кэш 2 с), pid→container
  internal/gguf/               # чтение метаданных GGUF для ctx_max
  internal/health/             # HTTP-пробы по каталогу
  internal/httpapi/            # роутер, JSON, ошибки, токен для CI
  internal/reconcile/          # 15-секундный цикл, drift
  config/services.yaml
  config/secrets.env.example
  deploy/switchd.container
  deploy/Dockerfile
  docs/spec-service-switch.md  # этот файл
  Makefile                     # build, test, run, lint
```

Соответствие стилю остальных сервисов: Go 1.2x, `net/http` без фреймворков, `slog` JSON, табличные тесты,
никакого cgo, конфиг — YAML + env, порт только внутренний, `healthz`/`readyz` раздельно.
