# Как поднимать сервис

Коротко: **Makefile — меню для человека, systemd — тот, кто держит процесс, YAML и env — настройки.** Смешивать эти три слоя неудобно: секрет оказывается в истории shell, рестарт забывают, а `make run` начинают считать продакшеном.

## Три слоя

| Слой | Где | Зачем |
|---|---|---|
| Параметры | `config/*.yaml`, `config/secrets.env`, переменные окружения | Что запускать и с какими лимитами. Меняется без правки кода |
| Меню | `Makefile` | Одинаковые команды локально и в CI: `make test`, `make run`, `make check` |
| Надзор | `deploy/switch.service` или контейнер | Рестарт, лог, старт после ребута |

`make` не умеет нормально жить в фоне. Нет рестарта, нет журнала, терминал закрылся — процесс умер. Поэтому `make run` только для ручной сессии за ноутбуком. На хосте с GPU ставится user-unit.

## Makefile, который не мешает

Рабочий файл — в корне. Он специально короткий.

1. **Цель = одно действие.** `test` запускает pytest, `run` — API, `check` — каталог. В рецепте нет `if` на десять строк: сложное уходит в `switch-service`.
2. **Настройки сверху и переопределяются снаружи.** `make run PORT=8096 CONFIG=config/services.homelab.example.yaml RUNTIME=podman` не требует правки файла.
3. **`.PHONY` и `help`.** У каждой цели комментарий `## ...`, `make` без аргументов печатает список. Так не нужно помнить имена.
4. **Зависимость, а не копипаста.** `test` и `run` зависят от `$(BIN)/python`, venv создаётся один раз.
5. **Секретов в Makefile нет.** Ни токенов, ни `-e HF_TOKEN=...`. Файл `config/secrets.env` (права `0600`) подключает systemd через `EnvironmentFile=`. В git лежит только `secrets.env.example`.
6. **Идемпотентность.** `make venv` и `make check` можно повторять. `make clean` удаляет venv и локальную БД `var/`, не системные каталоги.

Чего в Makefile не класть:

- `podman rm -f` чужих контейнеров «на всякий случай»;
- бесконечный цикл health-check (это reconciler внутри процесса);
- сборку чужих образов моделей — у них свои репозитории;
- `sudo`.

`just` или `task` делают то же меню чуть чище, но `make` уже есть везде и для этого проекта его хватает. Отдельный `docker-compose up` имеет смысл, когда появляются зависимости (база, брокер). Здесь база — один файл SQLite, поэтому compose не является основным путём. `deploy/compose.yaml` оставлен как запасной вариант.

## Ручной режим

Из корня репозитория:

```bash
make venv
make check
make run
```

В другом терминале:

```bash
.venv/bin/switch-service status
.venv/bin/switch-service state alpha gpu
.venv/bin/switch-service state alpha cpu
.venv/bin/switch-service state alpha off
.venv/bin/switch-service logs alpha --tail 50
```

`state` ждёт завершения и печатает итог. `--no-wait` только ставит переход в очередь. `--evict beta` разрешает остановить beta, если иначе не хватает памяти. `--force` нужен для профиля с `available: false` и для сервиса с `preemptible: false`.

Демо-каталог `config/services.yaml` не смотрит на реальную машину (`sample: fixture`) и не вызывает podman (`RUNTIME=fake` по умолчанию). Цифры RAM/VRAM в `status` — учебные, зато треугольник и отказы `409` видны сразу.

Прямой вызов без make, если так удобнее:

```bash
SWITCH_RUNTIME=fake SWITCH_DB=var/switch.sqlite \
  .venv/bin/switch-service serve --host 127.0.0.1 --port 8095 --config config/services.yaml
```

Тот же бинарь из другого сервиса на Python можно не поднимать сетью, а импортировать `SwitchCore` (см. README).

## Хост с GPU

1. Каталог — копия `config/services.homelab.example.yaml`, поправленная под реальные образы и пути. `sample: live`.
2. Секреты в `config/secrets.env`, `chmod 0600`.
3. Сокет: `systemctl --user enable --now podman.socket`.
4. Юнит: скопировать `deploy/switch.service` в `~/.config/systemd/user/`, поправить `WorkingDirectory` и `--config`.
5. Один раз: `loginctl enable-linger "$USER"`.
6. `systemctl --user enable --now switch.service`
7. Логи: `journalctl --user -u switch.service -f`

Порт в юните привязан к `127.0.0.1`. Наружу его выставляет уже существующий gateway, не этот сервис. Если нужен доступ машины из другой сети, задайте `SWITCH_TOKEN` в `EnvironmentFile` и не слушайте `0.0.0.0` без токена.

Контейнер (`deploy/switch.container`, `deploy/compose.yaml`) нужен, только если health-пробы обязаны резолвить DNS сетей podman. Control plane **не** получает `nvidia.com/gpu=all`: иначе он сам занимает VRAM, которую считает свободной. Если `nvidia-smi` из контейнера не видит драйвер, оставайтесь на user-unit — это нормальный, не запасной вариант.

## Чего не делать

- Не держать второй источник правды. Quadlet модели с `Restart=always` и compose, который поднимает тот же `container_name`, перетрут `off`.
- Не переключать CPU/GPU пересборкой образа. У llama.cpp это флаг `-ngl` / переменная окружения, один CUDA-образ умеет оба режима.
- Не вызывать `podman start` управляемых имён руками, кроме разбора аварии. После этого reconciler вернёт desired, и ручной запуск исчезнет.
- Не класть `--api-key` в командную строку, если у бинаря есть переменная окружения. У текущего llama-server её нет; это записано в каталоге, а не спрятано.
