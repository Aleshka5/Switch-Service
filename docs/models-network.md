# Одна сеть `models_network`, без compose

Четыре модели живут в одной bridge-сети, и создаёт её кто-то один. Владелец сети и владелец контейнеров — один и тот же
процесс: switch-service. Compose-файл не нужен, quadlet тоже: топология целиком описывается
`config/services.models-network.yaml`, а сеть и контейнеры создаются из него.

```
                        127.0.0.1:{18090,18091,18001,18000}   ← только health-пробы
                                     │
 ┌────────────────────────── models_network (bridge, dns) ──────────────────────────┐
 │  ocr:8090      embedding:8091      qwen38:4001      qwen-image:8000               │
 │      ▲               ▲                  ▲                  ▲                     │
 └──────┼───────────────┼──────────────────┼──────────────────┼─────────────────────┘
        │               │                  │                  │
  rag-chat_processor_1  rag-chat_chat_1   gateway            gateway
```

## Что в сети

| Сервис | Имя внутри сети | Внутренний порт | Health-проба (loopback) | Имя контейнера |
|---|---|---|---|---|
| OCR | `http://ocr:8090` | 8090 | `http://127.0.0.1:18090/health` | `ocr` |
| Embedder | `http://embedding:8091` | 8091 | `http://127.0.0.1:18091/health` | `embedding` |
| LLM | `http://qwen38:4001` | 4001 | `http://127.0.0.1:18001/health` | `qwen38` |
| Image Gen | `http://qwen-image:8000` | 8000 | `http://127.0.0.1:18000/healthz` | `qwen-image` |

Потребители, которым нужны эти имена, подключаются к сети один раз: `make network` (скрипт
`scripts/models-network.sh up`) добавляет в неё `gateway`, `rag-chat_processor_1`, `rag-chat_chat_1`.
Список переопределяется: `CONSUMERS="gateway" make network`.

Имена плоские: `ocr`, а не `ocr.models_network`. Полное имя `probe-alpha.models_network` не резолвится (проверено).

## Состояние на хосте (после миграции)

| Сервис | Контейнер | Образ | Режим | Как задаётся режим |
|---|---|---|---|---|
| `ocr` | `ocr` | `localhost/rag-chat_ocr:cuda` | `cpu` или `gpu` | env `OCR_LLAMA_NGL` (`0` / `999`) |
| `embedding` | `embedding` | `localhost/rag-chat_embedding:cuda` | `cpu` или `gpu` | env `EMBEDDING_LLAMA_NGL` (`0` / `999`) |
| `llm` | `qwen38` | `ghcr.io/ggml-org/llama.cpp:server-cuda12` | `gpu` (cpu только с `force`) | `-ngl -1` в command |
| `image` | `qwen-image` | `localhost/qwen-image-service:latest` | только `gpu` | — (CPU-профиля нет) |

Один CUDA-образ на сервис обслуживает оба режима: `-ngl 0` не переносит ни одного слоя на карту. Поэтому переключение
CPU↔GPU — это пересоздание контейнера с другим env, а не пересборка. Проверено на живой машине: в `cpu` llama-server
запускается с `-ngl 0`, в `gpu` — с `-ngl 999` и занимает 2680 MiB (OCR) / 690 MiB (embedding) VRAM.

Управляет всем `switch.service` (юзер-юнит, `127.0.0.1:8095`, `SWITCH_DB` в `~/.local/share/switch-service/state.db`).
`qwen-image` больше не квадлет: файл `~/.config/containers/systemd/qwen-image.container` удалён, `Restart=always` больше
не возвращает выключенный контейнер.

## Почему health-проба идёт через loopback, а не по имени

Измерено на этой машине (podman 5.7.0, rootless, backend netavark):

```
host -> 10.89.6.2:8080      curl: (28) Connection timed out      # IP контейнера с хоста недостижим
host -> probe-alpha:8080    Could not resolve host                # имён контейнеров в DNS хоста нет
probe-beta -> probe-alpha   200                                   # внутри сети всё работает
probe-gamma (порт 127.0.0.1:18090) -> 200                          # rootlessport на loopback работает
```

Отсюда две вещи:

1. Межсервисное общение — по имени внутри `models_network`. Это то, что нужно потребителям.
2. Проба живости — по опубликованному порту на `127.0.0.1`. Control plane работает на хосте (юзер-юнит), ему нужен
   `nvidia-smi` и `/proc/meminfo`, поэтому смотреть внутрь сети он не может.

Порт публикуется как `"127.0.0.1:18090:8090"` — только loopback, не `0.0.0.0`. В каталоге для этого у `ports`
появился третий формат: `ip:host:container`. Голый `container_port` публикуется на всех интерфейсах, `host:container`
— тоже, поэтому в этом каталоге так не пишем.

Альтернатива, если плоские имена проб важнее: гонять control plane контейнером в `models_network`. Тогда пробы идут по
DNS, но `nvidia-smi` из контейнера не видит драйвер без `nvidia.com/gpu=all`, а это ровно тот CDI-девайс, который
спецификация запрещает control plane. Поэтому по умолчанию — юзер-юнит на хосте, пробы по loopback.

## Почему не compose

| Проблема compose здесь | Как решается |
|---|---|
| `podman compose up` поднимает всё, что описано, и перетирает `off` | у моделей нет compose-файла; владелец — switch-service |
| Сеть создаётся с префиксом проекта (`rag_internal`, `deploy_auth_network`, `qwen_image_gen_network`) | одна именованная сеть `models_network`, её создаёт switchd по `host.network` |
| Переключение CPU/GPU в compose — это пересборка образа (`target: cuda`) | режим задаётся env/`-ngl`, контейнер пересоздаёт switchd |
| `deploy.resources` (GPU) на этом podman не работает | CDI-строка `nvidia.com/gpu=all` в `devices` |
| Желаемое состояние живёт в compose-файле, а не в БД | desired state в SQLite, reconciler догоняет после ребута |

`deploy/compose.yaml` в репозитории остаётся запасным вариантом для самого control plane, к моделям он отношения не имеет.

## Тома

Именованные тома podman создаёт сам при первом `create`, заранее их готовить не нужно. Важно только не потерять кэши:

| Том | Куда | Зачем |
|---|---|---|
| `rag-chat_ocr-model-cache` | `/var/cache/huggingface` | веса Surya (1.18 GiB + 0.19 GiB mmproj) |
| `rag-chat_embedding-model-cache` | `/var/cache/huggingface` | GGUF 330 MiB; том появился при миграции, первый старт скачал веса заново (~70 с) |
| `qwen-hf-cache` | `/data/huggingface` | Qwen-Image (десятки GiB) |
| `qwen-outputs` | `/data/outputs` | результаты генерации, `KEEP_OUTPUTS=200` |

Бинд `/media/aleksey/HardDisk/gguf_models:/models:ro` — для LLM, только чтение.

## Миграция (выполнена 2026-10-22)

До миграции OCR и embedder держал compose RAG-Chat (сеть `rag_internal`, дубли `rag-chat_ocr_1` и
`rag-chat_embedding_1`), image — quadlet с `Restart=always`, LLM — `podman run` из startup-скрипта. Что сделано:

1. `make network` — создана `models_network`, к ней подключены `gateway`, `rag-chat_processor_1`, `rag-chat_chat_1`.
2. Включён `podman.socket` (юзер-юнит) и `switch.service`; каталог — `config/services.models-network.yaml`.
3. Остановлен и удалён квадлет `qwen-image.container` (`systemctl --user stop qwen-image.service`, затем файл и
   `daemon-reload`), контейнер `qwen-image` удалён.
4. Образы пересобраны с целью `cuda` — по одному на сервис, оба режима через `*_LLAMA_NGL`:
   `podman build --target cuda -t localhost/rag-chat_ocr:cuda -f services/ocr/Dockerfile services/ocr` и то же для
   `localhost/rag-chat_embedding:cuda`.
5. `ocr` и `embedding` подняты через `switch-service state … cpu|gpu` и проверены: `-ngl` в argv совпадает с режимом,
   `/health` = 200 на loopback-порту, GPU-режим занимает 2680 MiB (OCR) и 690 MiB (embedding) VRAM.
6. Compose RAG-Chat больше не описывает `ocr`/`embedding`; `processor` и `chat` ходят к ним через `models_network`.

**Подводный камень, который проявился:** контейнеры `rag-chat_processor_1`, `rag-chat_chat_1` и
`rag-chat_rag-edge_1` были созданы podman-compose с зависимостью (`requires`) от старых `rag-chat_ocr_1` /
`rag-chat_embedding_1`, поэтому `podman rm` отказывался удалять модели («has dependent containers»). Эти три контейнера
статутные, их пришлось пересоздать: `podman rm -f <три>`, затем `podman compose up -d --no-build` (15 с, данные в томах
не тронуты). После пересоздания зависимостей нет.

Обратный путь: вернуть `ocr`/`embedding` в compose, восстановить квадлет, `podman compose up -d`. Второй источник правды
одновременно с switchd держать нельзя — reconciler вернёт своё состояние.

## Загрузка хоста

1. `systemctl --user enable --now podman.socket` — сокет, без него control plane не управляет контейнерами.
2. `loginctl enable-linger aleksey` — юзер-юниты живут без логина.
3. `systemctl --user enable --now switch.service` — юнит из `deploy/switch.service`, каталог в `--config`.
4. Сеть и контейнеры создаются при первом переходе; `desired_state` в SQLite переживает ребут, `off` остаётся `off`.

Сеть после ребута уже существует (podman хранит её в `~/.local/share/containers/storage/networks`), но код всё равно
проверяет её перед созданием контейнера — это одна идемпотентная проверка на процесс.

## Диагностика

| Команда | Что показывает |
|---|---|
| `make doctor` | rootless, сокет, GPU, `MemAvailable`, наличие образов, занятость health-портов, загрузку каталога |
| `make network-status` | подсеть и DNS сети, состав, ответы health-портов |
| `make network` | идемпотентно: сеть + потребители |
| `.venv/bin/switch-service status` | бюджеты RAM/VRAM и вершины треугольников |

Частые отказы:

- `RUNTIME_UNAVAILABLE` — нет сокета: `systemctl --user status podman.socket`.
- `IMAGE_MISSING` — образа нет локально, `podman image exists <image>` скажет какого.
- health-порт занят чужим процессом — `ss -ltnp | grep 18090`; публикуйте другой порт в каталоге.
- `409 CANNOT_EVICT` — пытались вытеснить `preemptible: false` без `force` (LLM).
