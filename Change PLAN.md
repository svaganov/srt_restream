# Отчёт аудита и план исправления `svaganov/srt_restream`

## Резюме

Аудит закреплён за [`main@66da73a`](https://github.com/svaganov/srt_restream/commit/66da73ac6e422d4dba408772a435b07640a739ce), 14 июля 2026 года.

Вердикт: архитектура понятна и подходит как прототип, но текущую версию нельзя безопасно публиковать в интернете или считать production-ready. Критичны аутентификация, управление FFmpeg, открытый внутренний UDP-транспорт и Docker-конфигурация.

Положительные стороны: `Popen` вызывается без `shell=True`, outputs изолированы отдельными FFmpeg-процессами, конфигурация сохраняется в SQLite, upload-пути не зависят от имени файла.

## Подтверждённые проблемы

### P0 — блокируют любое production-развёртывание

- Известные fallback-ключи позволяют самостоятельно подписать JWT от имени `admin`, а пустая установка автоматически создаёт `admin/admin`: [auth.py](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/auth.py#L12-L16), [создание пользователя](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/auth.py#L88-L97). Объявленные `ADMIN_*` фактически не используются.

- `srt_url` не валидируется и напрямую передаётся FFmpeg как input/output. Это разрешает другие протоколы, SSRF, чтение media-файлов и перезапись доступных файлов. Риск усиливают root-контейнер и `network_mode: host`: [API-модели](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/api.py#L242-L251), [FFmpeg command](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L487-L500), [Compose](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/docker-compose.yml#L11-L23).

- Внутренние media-сокеты bind-ятся к `0.0.0.0`. При host networking посторонний UDP-пакет может подменить feed либо вызвать restart storm без прохождения SRT-аутентификации: [mixer](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L205-L211), [splitter](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L318-L325).

### P1 — исправить до первого релиза

- Заявленный full passthrough теряет дополнительные дорожки из-за отсутствия `-map 0`; audio-only input падает на обязательном `-map 0:v`: [build_input_cmd](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L440-L456).

- Slate работает без `-re`/`-readrate 1`. Контрольная проверка сформировала 10 секунд потока за 0,549 секунды, создавая примерно 18-кратную нагрузку и UDP burst: [build_slate_cmd](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L458-L485).

- Callback, fast watcher и health loop независимо перезапускают один output. Возможны дублированные и «воскрешённые» после Stop/Delete процессы; retry counter обнуляется после каждого spawn и не ограничивает restart storm: [restart](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L557-L577), [health loop](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L797-L887).

- Внутренние порты вычисляются из DB ID пересекающимися диапазонами: например, `raw(101) == live(1)`. Ошибка bind завершает thread, но API всё равно сообщает успешный запуск: [port layout](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L391-L406), [ensure](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L503-L520).

- SRT statistics штатно не работают: код ожидает отсутствующий Windows `.exe`, Docker его не устанавливает, а `-statsout -` создаёт файл `backend/-`. Официальная документация указывает, что stdout используется без `-statsout`, и предупреждает, что `srt-live-transmit` — пример, не production-компонент: [proxy](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/srt_input_proxy.py#L20-L20), [официальная документация SRT](https://github.com/Haivision/srt/blob/master/docs/apps/srt-live-transmit.md).

- Stored XSS через произвольный `output.mode` сочетается с JWT в `localStorage` и query string thumbnail/WebSocket: [рендеринг](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/frontend/static/js/app.js#L244-L272), [WebSocket token](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/frontend/static/js/app.js#L582-L588).

- `.dockerignore` отсутствует и даже исключён через `.gitignore`, а `COPY . .` может встроить `.env`, SQLite и `.git` в image layers: [Dockerfile](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/Dockerfile#L12-L17), [.gitignore](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/.gitignore#L38-L39).

- Healthcheck всегда получает `401`, поскольку вызывает защищённый `/api/stats` без токена: [Compose](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/docker-compose.yml#L27-L33).

- `python-multipart==0.0.6` содержит несколько удалённо вызываемых parser DoS, включая [CVE-2024-24762](https://osv.dev/vulnerability/GHSA-2jv5-9r88-3w3p) и [CVE-2026-42561](https://osv.dev/vulnerability/GHSA-pp6c-gr5w-3c5g). Upload/import читаются целиком в RAM и не ограничивают число запускаемых процессов.

- Нет startup reconciliation, управляемого shutdown и очистки process groups. Сохранённый `is_active=true` после рестарта не восстанавливает поток; FFmpeg может остаться сиротой: [global manager](https://github.com/svaganov/srt_restream/blob/66da73ac6e422d4dba408772a435b07640a739ce/backend/stream_manager.py#L891-L892).

## План исправления

1. **Немедленное ограничение риска**

   - Не публиковать текущий порт 8080 и SRT-порты вне доверенной сети.
   - Ротировать JWT key, admin password и SRT passphrases; старые токены считать скомпрометированными.
   - Если образ уже собирался после создания `.env`, пересобрать его без cache и удалить старые образы/layers.

2. **Аутентификация и API**

   - Перейти с доступного JavaScript JWT на серверные opaque sessions: случайный токен хранится в БД только как hash, cookie — `HttpOnly`, `Secure`, `SameSite=Strict`, срок 8 часов.
   - Добавить CSRF token для mutating REST-запросов и строгую проверку `Origin` для REST/WebSocket; CORS для same-origin UI удалить.
   - Удалить `/auth/register`; первого администратора создавать одноразовой CLI-командой с Docker secret. Минимум пароля — 12 символов, hash — Argon2 через `pwdlib`.
   - Смена пароля и logout удаляют активные sessions; WebSocket перепроверяет session каждые 60 секунд.
   - Динамические данные frontend создавать DOM-методами с `textContent`; убрать inline handlers/scripts и включить CSP.

3. **Безопасная SRT-модель и Docker**

   - Ввести строгий тип `SrtUrl`: только `srt://`, обязательные host/port/mode, `mode ∈ {caller, listener}`, без userinfo/path/fragment. `mode` выводить из URL; отдельное request-поле удалить.
   - Listener-порты ограничить настраиваемым опубликованным диапазоном, по умолчанию `5000–5999/udp`; конфликтующие listener endpoints отклонять до spawn. Caller может обращаться к внутренним адресам, поскольку это штатная функция одного доверенного администратора.
   - Passphrase принимать отдельным write-only полем, хранить зашифрованно, не возвращать в API/export и редактировать во всех логах.
   - Удалить host networking. Внутренние UDP-сокеты bind-ить только к `127.0.0.1`; контейнер запускать non-root, `cap_drop: ALL`, `no-new-privileges`, read-only root FS и writable `/app/data`.
   - Добавить `.dockerignore`, selective `COPY`, pinned base digest, dependency lock с hashes и отдельные `/health/live`, `/health/ready`.

4. **Перестройка stream supervisor**

   - Создавать manager через FastAPI lifespan и поддерживать один Uvicorn worker.
   - Заменить несколько restart-механизмов единым single-flight supervisor: per-stream generation token, отмена pending restart при Stop/Delete, backoff `1/2/4/8/16/30s` с jitter, reset attempts только после 60 секунд стабильной работы.
   - Разделить persisted `desired_state` и наблюдаемый runtime status. На startup восстанавливать desired inputs, затем outputs; на shutdown останавливать process groups, сохраняя desired state.
   - Заменить ID-based UDP-порты централизованным allocator; bind/ready должны завершаться до успешного ответа API.
   - Relay запускать с явным `-map 0 -c copy`. Thumbnail вынести в независимый best-effort процесс, чтобы отсутствие video или ошибка JPEG не останавливали relay.
   - Slate сделать opt-in: добавить real-time pacing и разрешать только после проверки совместимого однодорожечного H.264/AAC live-профиля. Для остальных codec/layout показывать «slate unavailable» и не обещать бесшовность.
   - Убрать `srt-live-transmit` из критического пути первой hardened-версии. Endpoint SRT stats временно возвращает `available:false`; native libsrt telemetry оформить отдельным этапом.

5. **Данные, зависимости и эксплуатация**

   - Добавить Alembic-миграции для sessions, desired state и encrypted endpoint secrets; включить SQLite WAL/busy timeout.
   - Обновить согласованный FastAPI/Starlette/Pydantic stack, закрепить `python-multipart==0.0.32`; удалить `python-jose`, `passlib`, Jinja2, aiofiles и psutil, если они больше не используются.
   - Slate ограничить 10 MiB/16 MP, проверять реальный формат и перекодировать через Pillow; import ограничить 1 MiB, 100 inputs и 500 outputs, валидировать полностью до транзакции.
   - Добавить structured logging с redaction `passphrase`, JWT/session и query secrets; перестать печатать сырой FFmpeg stderr.
   - Исправить README: правильные URL `srt_restream`, Linux/Docker constraints, TLS/firewall, backup/restore, single-worker и точные ограничения passthrough/slate.

## Публичные интерфейсы

- `POST /api/auth/login` устанавливает session/CSRF cookies вместо возврата JWT.
- Добавляются `POST /api/auth/logout`, `/health/live`, `/health/ready`.
- Thumbnail и WebSocket используют session cookie; параметр `?token=` удаляется.
- Start/Stop становятся идемпотентными и возвращают `202` с `desired_state`; фактический статус приходит через REST/WebSocket.
- Input/output responses получают `desired_state`, `runtime_state`, `has_passphrase`; `is_active` сохраняется на один переходный релиз как deprecated alias.
- `mode` становится derived/read-only; несовпадение старого `mode` и URL возвращает `422`.
- Export по умолчанию не содержит passphrases.

## Проверка и критерии приёмки

- Auth: запуск без secrets/admin bootstrap невозможен; `admin/admin` и JWT со старыми ключами дают `401`; XSS payload отображается как текст; session отсутствует в URL, localStorage и логах.
- API: `file:`, `http:`, malformed SRT, mode conflict и listener-порт вне диапазона дают `422`; oversized upload/import — `413`.
- Streaming: multi-audio и audio-only проходят без потери relay; slate работает со `speed ≤ 1.05x`; 50 циклов disconnect/reconnect не увеличивают число процессов и открытых портов.
- Concurrency: одновременные restart/Stop/Delete никогда не создают более одного FFmpeg и не воскрешают остановленный stream.
- Security: внешние datagrams не достигают internal media plane; passphrase отсутствует в stderr/access logs.
- Persistence: Docker restart восстанавливает desired streams; SIGTERM завершает все дочерние процессы в пределах grace period.
- Container: healthcheck становится healthy, UID не root, `.env`/DB отсутствуют в `docker save`; `pip-audit`/OSV и image scan не содержат необработанных high/critical findings.
- CI: pytest для API/auth/supervisor, FFmpeg integration tests, frontend XSS/session smoke и Docker smoke запускаются на каждый PR.

## Допущения

- Основная среда — один Linux/Docker-узел за TLS reverse proxy.
- Модель доступа — один администратор.
- Приоритет — настоящий passthrough; универсальный бесшовный slate не заявляется.
- Аудит был read-only: Python-файлы прошли syntax compile, зависимости проверены live через PyPI/OSV, но Docker CLI в среде отсутствовал, поэтому image build и полноценный SRT end-to-end тест не выполнялись.
