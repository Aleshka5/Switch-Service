# Switch-Service

Control plane для инференса: у каждой модели три состояния, и из любого можно перейти в два других.

```
        off
       /   \
      /     \
    cpu ——— gpu
```

Перед стартом сервис проверяет бюджет RAM и VRAM. Клейм резервируется сразу, поэтому два параллельных запроса не могут оба решить, что память свободна. Желаемое состояние лежит в SQLite и поднимается после рестарта процесса.

Код — Python. Черновик ТЗ описывал Go и содержал противоречия (двойной учёт памяти, переход только через `off`, `command: ["--ngl"]` у Python-образов). Исправления и действующий контракт: [docs/spec-service-switch.md](docs/spec-service-switch.md).

## Запуск руками

```bash
make venv
make test
make run
```

Во втором терминале:

```bash
.venv/bin/switch-service status
.venv/bin/switch-service state alpha gpu
.venv/bin/switch-service state large gpu          # 409: 30720 + 4096 > 31232
.venv/bin/switch-service state large gpu --evict alpha --force
.venv/bin/switch-service state alpha cpu
```

`make run` по умолчанию ничего не запускает в podman и не читает реальную видеокарту: каталог `config/services.yaml` стоит в режиме `sample: fixture`. Так меню можно пощупать на любой машине.

## Реальные модели в одной сети

Топология четырёх моделей — `config/services.models-network.yaml`: одна bridge-сеть `models_network`, без compose и без
quadlet. Сеть создаёт сам сервис (`host.network` в каталоге), потребители подключаются один раз:

```bash
make doctor                                        # GPU, сокет, образы, порты, каталог
make network                                       # создать models_network и подключить gateway/processor/chat
make run RUNTIME=podman CONFIG=config/services.models-network.yaml
.venv/bin/switch-service state ocr gpu
make network-status
```

Почему пробы живости идут через `127.0.0.1`, а не по имени контейнера, и как мигрировать с compose — в
[docs/models-network.md](docs/models-network.md).

Почему Makefile — это меню, а не способ держать прод, и как поставить user-systemd на хост с GPU: [docs/operations.md](docs/operations.md).

Полезные ручки:

| Команда | Что делает |
|---|---|
| `make check` | читает каталог и печатает вершины |
| `make run PORT=8096` | другой порт, файл менять не нужно |
| `make run RUNTIME=podman CONFIG=config/services.homelab.example.yaml` | живой хост, свой каталог |
| `switch-service state NAME off` | выключить |
| `switch-service state NAME gpu --no-wait` | не ждать health |

`state` по умолчанию ждёт конца перехода. Одинаковое здоровое состояние повторно не трогает контейнер.

## Встроить в другой сервис

```python
from switch_service import SwitchCore

core = SwitchCore.open(
    "config/services.yaml",
    db_path="var/switch.sqlite",
    synchronous=True,
)
core.set_state("alpha", "cpu", actor="my-service")
print(core.snapshot())
```

HTTP (`switch-service serve`) нужен, когда вызывающий код — не Python. Пока не задан `SWITCH_TOKEN`, слушайте только localhost. Заголовок `X-Auth-Email` пишется в аудит как актор; авторизацию пользователей делает gateway перед этим сервисом, не сам сервис.

## Контракт состояний

- `off → cpu`, `off → gpu`, `cpu → gpu`, `gpu → cpu`, `cpu → off`, `gpu → off`.
- Нет профиля (у image нет CPU) или профиль помечен `available: false` — ребро всё равно есть, ответ `422` с причиной. `force` открывает только `available: false` и не отключает проверку памяти.
- `409` означает «не влезает» и не меняет контейнеры. В теле есть `required_mib`, `available_mib` и кого можно остановить.
- Пока переход идёт, следующий запрос получает `423`.

Демо-каталог: `alpha` и `beta` маленькие, `large` занимает почти всю учебную V100 и не preemptible. Пример реального хоста (числа из черновика от 2026-10-22, заново не измерялись): `config/services.homelab.example.yaml`.

## Макет API

`GET /api/services` — хост и сервисы. `POST /api/services/{id}/state` с телом `{"state":"gpu"}` — `202`. Ошибки: `{"error":{"code","message","details"}}`. Полная таблица — в спецификации.
