# Внутренний API Сбера

Получено реверс-инжинирингом приложения «Салют!» и проверено на живых
запросах. Официальной документации нет, схема может измениться.

## Авторизация

Каждый запуск проходит цепочку заново, токены ротируются.

1. `POST https://online.sberbank.ru:4431/CSAFront/api/service/oidc/v3/token`
   с `grant_type=refresh_token` возвращает `access_token` и новый
   `refresh_token`. Старый refresh-токен инвалидируется, новый пишется на диск.
   Формат тела: `application/x-www-form-urlencoded`. `client_id`:
   `b1f0f0c6-fcb0-4ece-8374-6b614ebe3d42`.
2. `GET https://mp-prom.salutehome.ru/v13/smarthome/token` с заголовком
   `Authorization: Bearer <access_token>` и обязательным `User-Agent` вида
   `Salute+prod%2F...`. Без User-Agent приходит HTML 400 или 403.
   Ответ: `{"token": "<jwt>"}`.
   Для входа по SMS это единственный рабочий хост.
   `companion.devices.sberbank.ru/v13/smarthome/token` принимает только токены
   Sber ID и отдаёт HTML 403.
3. Запросы на `https://gateway.iot.sberdevices.ru/gateway/v1/...` с заголовком
   `x-auth-jwt: <jwt>`, а также `User-Agent`, `Accept: application/json` и
   `x-trace-id`.

Срок действия JWT проверяется локально по полю `exp`. На ответ 401 токены
обновляются и запрос повторяется один раз.

Вход по SMS:

- `POST https://online.sberbank.ru/CSAFront/uapi/v2/authenticate` с телом
  `identifier.phone`, `authenticator.sms_otp` и `channel.data.rsa_data`.
  Поле `rsa_data` обязательно, без него приходит ошибка. Ответ содержит `ouid`.
- `POST .../uapi/v2/verify` с `ouid` и кодом из SMS. Ответ содержит
  `response_data.authcode`.
- `POST .../oidc/v3/token` с `grant_type=authorization_code` и `code_verifier`
  (PKCE). Возвращает токены.

## Устройства

| Метод | Путь | Назначение |
|---|---|---|
| GET | `device_groups` | дома и группы |
| GET | `device_groups/tree?id=<group>` | устройства группы |
| GET | `devices/<id>/state` | состояние, массив `reported_state` |
| PUT | `devices/<id>/state` | команда, `{"desired_state": [...], "device_id": ...}` |

Элемент состояния:

```json
{"key": "light_brightness", "type": "INTEGER", "integer_value": "468",
 "bool_value": false, "enum_value": "", "color_value": null}
```

Числа приходят строками (`"468"`), поэтому разбор выполняется вручную.

Диапазоны:

| Ключ | Значение |
|---|---|
| `light_brightness` | 50-1000, в CLI проценты равны raw/10 |
| `light_colour_temp` | 0-1000, 0 тёплый |
| `light_colour` | `{h: 0-360, s: 0-1000, v: 100-1000}` |
| `light_mode` | `scene`, `music`, `adaptive`, `white`, `colour` |
| `light_scene` | `candle`, `arctic`, `romantic`, `sunset`, `dawn`, `christmas`, `fito` |

## Сценарии

| Метод | Путь | Назначение |
|---|---|---|
| GET/POST | `scenario/v2/scenario` | список и создание |
| GET/PUT/DELETE | `scenario/v2/scenario/<id>` | один сценарий |
| PATCH | `scenario/v2/scenario/<id>/active` | `{"is_active": bool}` |
| POST | `scenario/v2/scenario/<id>/run` | выполнить сейчас |
| POST | `scenario/v2/scenario/<id>/stop` | остановить |

Ограничения, найденные при проверке:

- в `steps` допускается один элемент, на два приходит `wrong steps`;
- поле `image` обязательно, без него приходит `bad image`;
- `is_active` при создании игнорируется, облако ставит `true`. Выключать нужно
  отдельным `PATCH .../active`;
- один сценарий содержит одно время и один набор действий, поэтому график
  раскладывается на несколько сценариев.

Минимальное тело создания:

```json
{
  "name": "Свет 09:30",
  "image": "https://img.iot.sberdevices.ru/...",
  "home_id": "<id дома из device_groups>",
  "timezone": "Asia/Krasnoyarsk",
  "steps": [{
    "condition": {
      "type": "CONDITIONS",
      "nested_conditions_data": {"relation": "OR", "conditions": [{
        "type": "TIME",
        "time_data": {"execute_at": null,
          "rrule": "DTSTART;TZID=Asia/Krasnoyarsk:20261105T093000\nRRULE:FREQ=DAILY;WKST=MO;BYDAY=MO,TU,WE,TH,FR,SA,SU;BYHOUR=9;BYMINUTE=30;BYSECOND=0"}
      }]}
    },
    "tasks": [{
      "type": "DEVICE_COMMAND",
      "start_delay": "0s",
      "device_command_data": {
        "device_id": "<id лампы>",
        "parent_id": "<id дома>",
        "categories_slugs": ["light"],
        "desired_state": [{
          "state": {"key": "light_brightness", "type": "INTEGER",
                    "integer_value": "850"},
          "relative": false,
          "mode": "RANGE_SET"
        }],
        "alter_state": [],
        "alter_state_ttl": ""
      }
    }]
  }]
}
```

Условие можно отправлять в плоском виде (`{"type": "TIME", ...}`). Облако само
обернёт его в `CONDITIONS/nested` и в таком виде вернёт при чтении.