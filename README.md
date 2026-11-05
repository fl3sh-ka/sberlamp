# sberlamp

Управление умными лампами Sber (SBDV-...) из терминала: питание, яркость,
температура, RGB, сцены, плавные переходы и график день/ночь. График можно
держать локально или загрузить в облако Сбера.

Неофициальный клиент. Работает через тот же приватный API, что приложение
«Салют!». Со Сбером не связан.

## Требования

- Python 3.10+
- лампа добавлена в приложение «Салют!» и находится онлайн
- прямой доступ к `online.sberbank.ru`, `salutehome.ru`, `sberdevices.ru`

## Установка

```bash
git clone https://github.com/fl3sh-ka/sberlamp.git ~/Projects/sberlamp
cd ~/Projects/sberlamp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p ~/.local/bin && ln -sf "$PWD/bin/lamp" ~/.local/bin/lamp
```

`~/.local/bin` должен быть в `PATH`. Ссылка указывает на скрипт в проекте,
поэтому путь установки может быть любым. Если проект перенесён, ссылку нужно
создать заново.

В Fedora из системного нужен только `git`: `sudo dnf install git`.

## Первый запуск

```bash
lamp setup
```

Вводится номер телефона, затем код из SMS, затем выбирается лампа из списка.
Токен сохраняется в `~/.config/sberlamp/refresh_token` с правами 600, id
устройства в `~/.config/sberlamp/config.json`. Привязка личная: каждый
пользователь выполняет `lamp setup` под своим аккаунтом, файлы в репозиторий
не попадают.

## Обновление

```bash
cd ~/Projects/sberlamp && git pull && .venv/bin/pip install -r requirements.txt
```

Симлинк в `~/.local/bin` остаётся рабочим.

## Команды

| Команда | Действие |
|---|---|
| `lamp on` / `off` / `toggle` | питание |
| `lamp status` | яркость, режим, цвет |
| `lamp b 40` | яркость 5-100 |
| `lamp up [N]` / `down [N]` | +-10% или N |
| `lamp warm` / `cool` / `temp 30` | температура: 0 тёплый, 100 холодный |
| `lamp rgb 255 120 0` | цвет |
| `lamp scene sunset` | candle, arctic, romantic, sunset, dawn, christmas, fito |
| `lamp adaptive` | адаптивный режим лампы |
| `lamp fade 20 15` | плавно до 20% за 15 минут |
| `lamp devices` | список ламп в аккаунте |

## График день/ночь

Опорные точки задаются в `~/.config/sberlamp/schedule.json`. Пример лежит в
`schedule.example.json`:

```json
{"anchors": [
  {"time": "00:00", "brightness": 10, "temp": 20},
  {"time": "09:30", "brightness": 85, "temp": 75},
  {"time": "22:30", "brightness": 25, "temp": 28}
]}
```

Между точками значения считаются линейно. Чем больше точек, тем плавнее.

Свой файл создаётся из примера:

```bash
mkdir -p ~/.config/sberlamp && cp schedule.example.json ~/.config/sberlamp/schedule.json
```

Без файла используется встроенный график с теми же точками.

Есть два способа применения графика. Одновременно включать не нужно.

| Способ | Команда | Нужен включённый компьютер |
|---|---|---|
| локально, проверка каждые 3 минуты | `lamp watch` или служба systemd | да |
| сценарии в облаке Сбера | `lamp cloud push` | нет |

`lamp auto` применяет текущее значение один раз. `lamp auto --dry-run`
показывает результат без отправки команд.

## Облачные сценарии

```
lamp cloud push      создать или пересоздать сценарии по schedule.json
lamp cloud push --step 30
                     разложить график на точки каждые 30 минут
lamp cloud list      состояние и ошибки
lamp cloud off|on    выключить или включить автосрабатывание
lamp cloud delete    удалить все сценарии графика
```

На каждую точку графика создаётся отдельный сценарий с именем `Свет HH:MM`.
Идентификаторы хранятся в `~/.config/sberlamp/cloud.json`.

Чем больше точек, тем чаще меняется свет и тем плавнее переходы. Флаг `--step`
считает промежуточные значения между опорными точками, поэтому в
`schedule.json` их можно оставить мало. Шаг 30 минут даёт 48 сценариев в сутки,
час 24, два часа 12. Много сценариев заметно удлиняет список в приложении
«Салют!», на работу лампы это не влияет.

Сценарий срабатывает один раз в своё время, между точками лампа не
управляется. На каждой точке выставляется режим `white`, поэтому включённый
вручную цвет сбросится на ближайшей точке. Отключить автоматику на любое
время: `lamp cloud off`.

Локальная служба как альтернатива облаку:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/lamp-auto.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now lamp-auto.service
```

Логи: `journalctl --user -u lamp-auto -n 20`.

## Горячие клавиши

Команды `lamp` можно вызывать из биндов любого WM или DE:

```
Mod+F1  lamp down 10
Mod+F2  lamp up 10
Mod+F3  lamp toggle
```

## Как это работает

1. `online.sberbank.ru:4431/CSAFront/api/service/oidc/v3/token` меняет
   refresh-токен на access. Токены ротируются при каждом запуске.
2. `mp-prom.salutehome.ru/v13/smarthome/token` меняет access на SmartHome-токен.
   Заголовок `User-Agent` обязателен.
3. Запросы идут на `gateway.iot.sberdevices.ru/gateway/v1/...` с заголовком
   `x-auth-jwt`.

Эндпоинты, диапазоны значений и схема сценария описаны в [docs/api.md](docs/api.md).

## Ограничения

- API неофициальный и может измениться.
- Команды идут через облако, задержка около 0,5-1 с.
- Локальный график работает только при включённом компьютере.

## Диагностика

- `Токен не найден или истёк`: выполнить `lamp setup`.
- Таймаут или 403: облако Сбера недоступно. Обычно мешает VPN, если трафик
  идёт через зарубежный выход. Домены `salutehome.ru` и `sberdevices.ru`
  должны идти напрямую.
- Доступ отозван: `lamp setup` заново либо удалить
  `~/.config/sberlamp/refresh_token`.

## Лицензия

MIT, см. [LICENSE](LICENSE).
