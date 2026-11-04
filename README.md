# sberlamp — Sber smart bulb CLI

CLI for Sber smart lamps (SBDV-…): on/off, brightness, colour temperature, RGB,
scenes, smooth fades and a day/night schedule — either local (systemd) or
**inside the Sber cloud**, so it keeps working with the laptop off.

Unofficial: it speaks the same private API as the «Салют!» mobile app; there is
no local protocol in these bulbs. Not affiliated with Sber, use at your own risk.

```bash
git clone https://github.com/fl3sh-ka/sberlamp.git ~/Projects/sberlamp
cd ~/Projects/sberlamp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p ~/.local/bin && cp bin/lamp ~/.local/bin/ && chmod +x ~/.local/bin/lamp

lamp setup            # phone → SMS → pick your lamp
lamp status
lamp b 40             # brightness 5–100
lamp rgb 255 120 0
lamp scene sunset
lamp cloud push       # upload the schedule to Sber cloud (no PC required)
lamp cloud off        # freeze automation, keep manual control
```

What makes it different from other Sber-bulb tools: the cloud schedule. Sber's
`scenario/v2` API is undocumented, so the payload shape (one step per scenario,
RRULE conditions, `image` required, `is_active` ignored on create) was reverse
engineered and written down in
[Как это работает](#как-это-работает-для-желающих-покопать).
Needs Python 3.10+ and Linux/macOS.

---

## Русский

CLI для умных ламп Sber (SBDV-…): включение, яркость, температура, цвет,
сцены, плавное затухание и график «день/ночь» — локальный или в облаке Сбера.

Неофициально: работает через тот же облачный API, что приложение «Салют!».
Локального протокола у этих ламп нет, поэтому нужен интернет и сама лампа
должна быть онлайн. Используй на свой риск: API внутренний и может измениться.

## Установка

```bash
git clone https://github.com/fl3sh-ka/sberlamp.git ~/Projects/sberlamp
cd ~/Projects/sberlamp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ~/.local/bin
cp bin/lamp ~/.local/bin/lamp && chmod +x ~/.local/bin/lamp
```

`~/.local/bin` в Fedora уже в `PATH`. Если ставишь проект в другое место —
поправь пути в `bin/lamp`.

## Первый запуск

```bash
lamp setup
```

Спросит номер телефона (как в «Салют!»), пришлёт SMS, затем покажет список
ламп — выбрать нужную. Токен сохранится в `~/.config/sberlamp/refresh_token`
(права 600), ID лампы — в `~/.config/sberlamp/config.json`.

## Команды

```bash
lamp on | off | toggle      # включить / выключить / переключить
lamp status                 # что сейчас: яркость, режим, цвет
lamp b 40                   # яркость 5–100
lamp up | lamp up 20        # ярче на 10 (или на 20)
lamp down | lamp down 5     # тусклее
lamp warm | cool            # тёплый / холодный белый
lamp temp 30                # 0 — тёплый, 100 — холодный
lamp rgb 255 120 0          # цвет в RGB
lamp scene sunset           # candle, arctic, romantic, sunset, dawn, christmas, fito
lamp adaptive               # адаптивный свет
lamp fade 20 15             # плавно до 20% за 15 минут (по умолчанию 10)
lamp devices                # все лампы в аккаунте
```

У SBDV-00115 нет таймера сна, поэтому команды `timer` нет. Диапазоны API:
яркость 50–1000, температура 0–1000, цвет `s` 0–1000, `v` 100–1000.

## Свой «адаптив»: яркость и температура по часам

У режима `adaptive` кривая зашита в прошивку лампы, менять её через API нельзя.
Поэтому график задаётся снаружи:

```bash
lamp schedule               # показать график и что должно быть сейчас
lamp auto --dry-run         # что было бы, ничего не меняя
lamp auto                   # применить прямо сейчас
lamp watch                  # следить постоянно (это запускает служба)
```

Опорные точки лежат в `~/.config/sberlamp/schedule.json` (если файла нет —
стандартный график):

```json
{
  "anchors": [
    {"time": "00:00", "brightness": 6,  "temp": 20},
    {"time": "06:30", "brightness": 10, "temp": 25},
    {"time": "09:00", "brightness": 85, "temp": 80},
    {"time": "18:00", "brightness": 55, "temp": 45},
    {"time": "22:30", "brightness": 12, "temp": 25}
  ]
}
```

Между точками значения считаются линейно, так что переходы плавные.
`auto`/`watch` не трогают лампу, если она выключена или включена сцена/цвет
(перебить — `lamp auto --force`). Если яркость и температура уже совпадают
с графиком, запрос на изменение не отправляется.

## Облачные сценарии (ноутбук не нужен)

Тот же график можно заливать в облако Сбера: там он срабатывает сам, даже
когда ноутбук выключен. Облако принимает **один шаг на сценарий**, поэтому
на каждую опорную точку создаётся отдельный сценарий с именем `Свет HH:MM`.

```bash
lamp cloud push             # создать/пересоздать по schedule.json
lamp cloud list             # что создано и активно ли
lamp cloud off | on         # выключить/включить автосрабатывание всех сразу
lamp cloud delete           # снести все сценарии графика
```

ID созданных сценариев хранятся в `~/.config/sberlamp/cloud.json`.

Важно про ручные правки: сценарий — разовый выстрел в свой момент времени,
между точками облако лампу не трогает. Сделал ярче в 10:30 — так и будет до
13:00. Но каждая точка ставит режим `white`, поэтому цвет, включённый вручную,
сбросится на ближайшей точке. Нужно «заморозить» ручной свет надолго —
`lamp cloud off`, потом `lamp cloud on`.

Не включай одновременно облачные сценарии и локальную службу `lamp watch`:
они будут спорить (служба возвращает значение каждые 3 минуты).

Служба (user-юнит, включить один раз):

```bash
mkdir -p ~/.config/systemd/user
cp ~/Projects/sberlamp/systemd/lamp-auto.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lamp-auto.service
```

Проверить: `systemctl --user status lamp-auto` и `journalctl --user -u lamp-auto -n 20`.
Остановить: `systemctl --user disable --now lamp-auto.service`.

## Горячие клавиши (niri)

Пример для `~/.config/niri/config.kdl`:

```kdl
binds {
    Mod+F1 { spawn "sh" "-c" "lamp down 10"; }
    Mod+F2 { spawn "sh" "-c" "lamp up 10"; }
    Mod+F3 { spawn "sh" "-c" "lamp toggle"; }
}
```

## Если что-то сломалось

- `Токен не найден или истёк` — снова `lamp setup`.
- Ошибка сети / 403 / таймаут — облако Сбера не отвечает. Частая причина: весь
  трафик идёт через зарубежный VPN, а российские CDN Сбера такие адреса
  игнорируют. Домены `salutehome.ru` и `sberdevices.ru` должны идти напрямую
  (в NekoBox/nekoray — список `direct_domain` профиля).
- Сбер может изменить внутренний API — тогда проект сломается. Зависимостей
  всего одна (`aiohttp`), править придётся `SberLampAPI` в `lamp.py`.
- Пароль Сбер ID нигде не хранится, только refresh-токен. Отозвать доступ
  можно сменой пароля или удалением `~/.config/sberlamp/refresh_token`.

## Как это работает (для желающих покопать)

Авторизация в три шага, каждый запуск обновляет токены по цепочке:

1. `POST https://online.sberbank.ru:4431/CSAFront/api/service/oidc/v3/token` —
   refresh-токен меняется на access (ротация: старый инвалидируется).
2. `GET https://mp-prom.salutehome.ru/v13/smarthome/token` — access меняется на
   SmartHome-токен. Заголовок `User-Agent` обязателен, иначе HTML 403.
   Для входа по SMS это единственный рабочий хост: `companion.devices.sberbank.ru`
   принимает только токены Sber ID.
3. `https://gateway.iot.sberdevices.ru/gateway/v1/…` — сам API, заголовок
   `x-auth-jwt`.

Полезные эндпоинты:

| Метод | Путь | Что делает |
|---|---|---|
| GET | `device_groups` | список домов/групп |
| GET | `device_groups/tree?id=<group>` | устройства группы |
| GET | `devices/<id>/state` | состояние (`reported_state[]`) |
| PUT | `devices/<id>/state` | команда: `{"desired_state": [...], "device_id": ...}` |
| GET/POST | `scenario/v2/scenario` | список / создание сценариев |
| GET | `scenario/v2/scenario/<id>` | один сценарий |
| PATCH | `scenario/v2/scenario/<id>/active` | `{"is_active": bool}` |
| POST | `scenario/v2/scenario/<id>/run` | выполнить сейчас |
| DELETE | `scenario/v2/scenario/<id>` | удалить |

Диапазоны: `light_brightness` 50–1000, `light_colour_temp` 0–1000 (0 тёплый),
`light_colour` — `{h: 0–360, s: 0–1000, v: 100–1000}`. В CLI проценты — это
raw/10. Значения в `reported_state` приходят строками (`"integer_value": "468"`),
поэтому разбор сделан «сырым» JSON, без строгих моделей.

Сценарий принимает **ровно один шаг** (`"wrong steps"` на два), условие —
`TIME` с RRULE:

```json
{
  "name": "Свет 09:30",
  "timezone": "Asia/Krasnoyarsk",
  "is_active": true,
  "image": "https://img.iot.sberdevices.ru/…",
  "steps": [{
    "condition": {
      "type": "CONDITIONS",
      "nested_conditions_data": {"relation": "OR", "conditions": [
        {"type": "TIME", "time_data": {"execute_at": null,
         "rrule": "DTSTART;TZID=Asia/Krasnoyarsk:20261105T093000\nRRULE:FREQ=DAILY;WKST=MO;BYDAY=MO,TU,WE,TH,FR,SA,SU;BYHOUR=9;BYMINUTE=30;BYSECOND=0"}}
      ]}
    },
    "tasks": [{
      "type": "DEVICE_COMMAND",
      "start_delay": "0s",
      "device_command_data": {
        "device_id": "<id>", "parent_id": "<home id>", "categories_slugs": ["light"],
        "desired_state": [{"state": {"key": "light_brightness", "type": "INTEGER",
                                     "integer_value": "850"}, "relative": false,
                           "mode": "RANGE_SET"}],
        "alter_state": [], "alter_state_ttl": ""
      }
    }]
  }]
}
```

Нюанс: `is_active` при создании игнорируется — облако ставит `true`, выключать
надо отдельным `PATCH …/active`. Поле `image` обязательно (иначе `bad image`).

## Альтернатива

Home Assistant + интеграция `dzerik/ha-sberhome` (или `altfoxie/ha-sberdevices`)
даёт веб-интерфейс и готовые автоматизации, но требует постоянно работающего
сервера и ходит в тот же облачный API — локального протокола у ламп нет.