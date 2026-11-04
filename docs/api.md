# Внутренний API Сбера

Всё получено реверс-инжинирингом приложения «Салют!» и живыми запросами.
Схема не документирована и может измениться.

## Авторизация

Каждый запуск проходит цепочку заново, токены ротируются:

1. `POST https://online.sberbank.ru:4431/CSAFront/api/service/oidc/v3/token`
   - `grant_type=refresh_token` + сохранённый refresh-токен → `access_token`,
     `refresh_token` (старый refresh инвалидируется, новый пишется на диск);
   - `Content-Type: application/x-www-form-urlencoded`, `client_id`
     `b1f0f0c6-fcb0-4ece-8374-6b614ebe3d42`.
2. `GET https://mp-prom.salutehome.ru/v13/smarthome/token`
   - `Authorization: Bearer <access_token>`, обязателен `User-Agent` вида
     `Salute+prod%2F…`; без него — HTML 400/403;
   - ответ: `{"token": "<jwt>"}`;
   - для входа по SMS это единственный рабочий хост.
     `companion.devices.sberbank.ru/v13/smarthome/token` принимает только
     токены Sber ID и отдаёт HTML 403.
3. Запросы к `https://gateway.iot.sberdevices.ru/gateway/v1/…` с заголовком
   `x-auth-jwt: <jwt>`. Дополнительно: `User-Agent`, `Accept: application/json`,
   `x-trace-id`.

JWT проверяется локально по `exp` (без проверки подписи). На `401` токены
обновляются и запрос повторяется один раз.

Вход по SMS:

- `POST https://online.sberbank.ru/CSAFront/uapi/v2/authenticate` — тело с
  `identifier.phone`, `authenticator.sms_otp`, `channel.data.rsa_data`
  (отпечаток браузера, без него ошибка) → `ouid`;
- `POST …/uapi/v2/verify` — `ouid` + код → `response_data.authcode`;
- `POST …/oidc/v3/token` — `grant_type=authorization_code` + `code_verifier`
  (PKCE) → токены.

## Устройства

| Метод | Путь | Назначение |
|---|---|---|
| GET | `device_groups` | дома и группы |
| GET | `device_groups/tree?id=<group>` | устройства группы |
| GET | `devices/<id>/state` | состояние, `reported_state[]` |
| PUT | `devices/<id>/state` | `{"desired_state": [...], "device_id": ...}` |

Состояние — массив элементов:

```json
{"key": "light_brightness", "type": "INTEGER", "integer_value": "468",
 "bool_value": false, "enum_value": "", "color_value": null}
```

Значения приходят **строками** (`"468"`), поэтому парсинг делается вручную,
без строгих моделей.

Диапазоны:

| Ключ | Значение |
|---|---|
| `light_brightness` | 50–1000 (в CLI проценты = raw/10) |
| `light_colour_temp` | 0–1000, 0 — тёплый |
| `light_colour` | `{h: 0–360, s: 0–1000, v: 100–1000}` |
| `light_mode` | `scene`, `music`, `adaptive`, `white`, `colour` |
| `light_scene` | `candle`, `arctic`, `romantic`, `sunset`, `dawn`, `christmas`, `fito` |

## Сценарии

| Метод | Путь | Назначение |
|---|---|---|
| GET/POST | `scenario/v2/scenario` | список / создание |
| GET/PUT/DELETE | `scenario/v2/scenario/<id>` | один сценарий |
| PATCH | `scenario/v2/scenario/<id>/active` | `{"is_active": bool}` |
| POST | `scenario/v2/scenario/<id>/run` | выполнить сейчас |
| POST | `scenario/v2/scenario/<id>/stop` | остановить |

Ограничения, выясненные экспериментально:

- в `steps` допускается **ровно один элемент**, иначе `wrong steps`;
- поле `image` обязательно (иначе `bad image`);
- `is_active` при создании игнорируется — облако ставит `true`, выключать
  нужно отдельным `PATCH …/active`;
- одно срабатывание = одно время и один набор действий, поэтому график
  раскладывается на несколько сценариев.

Минимальное тело создания:

```json
{
  "name": "Свет 09:30",
  "image": "https://img.iot.sberdevices.ru/…",
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

`condition` можно прислать плоским (`{"type": "TIME", …}`) — облако само
обернёт в `CONDITIONS/nested` и вернёт в таком виде при чтении.