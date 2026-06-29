# План внедрения SRT-статистики для input'ов

## Цель
Получать детальную SRT-статистику входящего потока (state, peer, lost/recovered, ACK/NAK, RTT, buffer, latency и др.) аналогично Makito x4 Decoder, используя `srt-live-transmit` как SRT→UDP шлюз, и показывать её в сворачиваемой секции карточки input'а.

## Архитектура
- `srt-live-transmit.exe` принимает/устанавливает SRT-соединение по URL пользователя и передаёт MPEG-TS на локальный UDP-порт `30000+id`.
- FFmpeg input остаётся без изменений в логике, но вместо `srt://...` читает `udp://127.0.0.1:30000+id`.
- Прокси пишет SRT-статистику в stdout в формате JSON; бэкенд парсит и сохраняет.

## Шаги

### 1. Подготовка SRT-инструмента
- Скачать `srt-live-transmit.exe` (Windows x64) с https://jeremylee.sh/bins/ в `backend/tools/`.
- Проверить SHA1 из страницы.
- Добавить `backend/tools/` в `.gitignore`.

### 2. Модуль `backend/srt_input_proxy.py`
Класс `SrtInputProxy(stream_id, srt_url, live_port)`:
- Парсит `srt_url` (mode, latency, passphrase и т.д.).
- Формирует команду:
  ```
  srt-live-transmit.exe srt_url udp://127.0.0.1:live_port
      -statsout - -statspf json -s <freq> -buffering 1 -chunk 1316
  ```
- Запускает процесс, читает stdout, распознаёт JSON-строки со статистикой.
- Хранит последние данные в `self.stats` и флаг `self.connected`.
- Имеет `start()`, `stop()`, `get_stats()`.
- Авто-рестарт при падении (до 5 попыток).

### 3. Изменения `backend/stream_manager.py`
- В `start_input()`:
  - Если URL начинается с `srt://`, запустить `SrtInputProxy` перед FFmpeg.
  - Изменить `build_input_cmd()` на чтение с `udp://127.0.0.1:{live_port}` (с сохранением thumbnail, кодирования и т.д.).
  - Сохранить прокси в `self.srt_proxies[stream_id]`.
- В `stop_input()`:
  - Остановить прокси.
- Добавить `get_input_srt_stats(stream_id)`.
- Добавить авто-рестарт прокси в `_health_check_loop`.

### 4. API `backend/api.py`
- Новый endpoint: `GET /api/inputs/{stream_id}/srt-stats`.
- Включить `srt_stats` в ответы `/stats` и WebSocket (`input_srt_stats`).
- Уменьшить интервал WebSocket с 2 до 1 секунды для обновления статистики раз в секунду.

### 5. Frontend
- В `renderStreamCard()` под readouts добавить секцию "SRT Statistics".
- По умолчанию свёрнута. Кнопка/иконка разворачивает/сворачивает.
- Рендерить таблицу ключ→значение из `input_srt_stats`.
- Обновление через существующий WebSocket.
- Поднять версии `style.css` и `app.js`.

### 6. Тестирование и откат
- Запустить input с SRT-источником, убедиться, что статистика приходит и отображается.
- Проверить, что rollback работает: `git checkout before-srt-proxy`.

## Риски и ограничения
- `srt-live-transmit` — сторонний бинарник; придётся доверять источнику или позже собрать свой.
- Частота выдачи статистики в `srt-live-transmit` привязана к числу пакетов (`-s`), не ко времени; подберём частоту под типичный битрейт.
- Для non-SRT input'ов логика не меняется.
