# sberlamp

CLI для умных ламп Sber (SBDV-…): питание, яркость, температура, RGB, сцены,
плавные переходы, график день/ночь — локально или в облаке Сбера, без сервера.

*Unofficial CLI for Sber smart bulbs. Uses the same private API as the
«Салют!» app. Not affiliated with Sber.*

## Требования

- Python 3.10+
- лампа добавлена в приложение «Салют!» и онлайн
- прямой доступ к `online.sberbank.ru`, `salutehome.ru`, `sberdevices.ru`

## Установка

```bash
git clone https://github.com/fl3sh-ka/sberlamp.git ~/Projects/sberlamp
cd ~/Projects/sberlamp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p ~/.local/bin && cp bin/lamp ~/.local/bin/ && chmod +x ~/.local/bin/lamp
```

`~/.local/bin` должен быть в `PATH`. Если проект лежит в другом каталоге —
поправить пути в `bin/lamp`.

## Первый запуск

```bash
lamp setup
```

Номер телефона → SMS-код → выбор лампы из списка. Сохраняется:
`~/.config/sberlamp/refresh_token` (права 600) и `~/.config/sberlamp/config.json`.

## Команды

| Команда | Действие |
|---|---|
| `lamp on` / `off` / `toggle` | питание |
| `lamp status` | состояние: яркость, режим, цвет |
| `lamp b 40` | яркость 5–100 |
| `lamp up [N]` / `down [N]` | ±10% (или N) |
| `lamp warm` / `cool` / `temp 30` | температура, 0 — тёплый, 100 — холодный |
| `lamp rgb 255 120 0` | цвет |
| `lamp scene sunset` | candle, arctic, romantic, sunset, dawn, christmas, fito |
| `lamp adaptive` | адаптивный режим лампы |
| `lamp fade 20 15` | плавно до 20% за 15 минут |
| `lamp devices` | список ламп в аккаунте |

## График день/ночь

Опорные точки в `~/.config/sberlamp/schedule.json` (пример —
`schedule.example.json`):

```json
{"anchors": [
  {"time": "00:00", "brightness": 10, "temp": 20},
  {"time": "09:30", "brightness": 85, "temp": 75},
  {"time": "22:30", "brightness": 25, "temp": 28}
]}
```

Между точками значения интерполируются линейно.

Способы применения (одновременно не включать):

| Способ | Команда | Нужен включённый ПК |
|---|---|---|
| локально, каждые 3 мин | `lamp watch` / служба systemd | да |
| в облаке Сбера | `lamp cloud push` | нет |

`lamp auto` — применить текущее значение один раз, `lamp auto --dry-run` —
показать без изменений.

## Облачные сценарии

```
lamp cloud push      создать/пересоздать сценарии по schedule.json
lamp cloud list      состояние и ошибки
lamp cloud off|on    тумблер автосрабатывания всех сразу
lamp cloud delete    удалить все
```

На каждую точку графика создаётся отдельный сценарий `Свет HH:MM`; ID — в
`~/.config/sberlamp/cloud.json`. Сценарий срабатывает один раз в своё время,
между точками облако лампу не трогает. Каждая точка ставит режим `white`,
поэтому ручной цвет сбрасывается на ближайшей точке. Пауза на любой срок —
`lamp cloud off`.

Локальная служба (альтернатива облаку):

```bash
mkdir -p ~/.config/systemd/user
cp systemd/lamp-auto.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now lamp-auto.service
```

Логи: `journalctl --user -u lamp-auto -n 20`.

## Горячие клавиши

Команды `lamp ...` можно повесить на любые бинды WM/DE, например:

```
Mod+F1  lamp down 10
Mod+F2  lamp up 10
Mod+F3  lamp toggle
```

## Как это работает

1. `online.sberbank.ru:4431/CSAFront/api/service/oidc/v3/token` — refresh-токен
   меняется на access (ротация при каждом запуске).
2. `mp-prom.salutehome.ru/v13/smarthome/token` — access меняется на
   SmartHome-токен; заголовок `User-Agent` обязателен.
3. `gateway.iot.sberdevices.ru/gateway/v1/…` — API, заголовок `x-auth-jwt`.

Эндпоинты, диапазоны значений и схема облачного сценария:
[docs/api.md](docs/api.md).

## Ограничения

- API неофициальный, может измениться без предупреждения.
- Команда идёт через облако: задержка ~0,5–1 с.
- Локальный график работает только при включённом ПК.

## Если не работает

- `Токен не найден или истёк` — выполнить `lamp setup`.
- Таймаут или 403 — облако Сбера недоступно. Частая причина: трафик идёт
  через зарубежный VPN; домены `salutehome.ru` и `sberdevices.ru` должны
  идти напрямую.
- Доступ отозван — `lamp setup` заново или удалить
  `~/.config/sberlamp/refresh_token`.

## Лицензия

MIT — см. [LICENSE](LICENSE).