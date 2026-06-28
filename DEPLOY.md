# Деплой моста на сервер (бесплатно)

Мост — это **постоянно работающий процесс** (держит WebSocket к MAX и
long-poll к Telegram). Поэтому нужен хост, который **не «засыпает»**.
Бесплатные «спящие» PaaS (Render free, Replit, обычный Heroku-стайл) **не
подойдут** — они рвут постоянное соединение.

## ⚠️ Важно про IP и токен

- Бэкенд MAX (`ws-api.oneme.ru`) может быть чувствителен к гео-IP. С зарубежного
  сервера соединение может не подняться — тогда понадобится прокси с российским
  IP. **С российским IP (домашнее устройство) проблем нет.**
- Токен MAX привязан к сессии. Если он одновременно используется и в браузере, и
  на сервере с сильно другого IP, MAX может разлогинить сессию. Для надёжности —
  получите свежий токен специально для сервера и не входите параллельно.

## Конфигурация (одинаково для всех вариантов)

Токены задаются переменными окружения (файл `.env`) — копировать `config.json`
не нужно. Минимум для входа:

- `MAX2TG_TELEGRAM_BOT_TOKEN` — токен бота от @BotFather.
- `MAX2TG_TELEGRAM_CHAT_ID` — ваш chat_id (личка с ботом; QR/SMS/2FA и команды).
- `MAX2TG_MAX_TOKEN` — на [web.max.ru](https://web.max.ru): `F12` → Console →
  `copy(JSON.parse(localStorage.__oneme_auth).token)` (нужен только для метода
  входа `token`; для `sms`/`qr` см. `MAX2TG_AUTH_METHOD` в `.env.example`).

Для **нормальной работы обязательно** включите режим тем — добавьте
`MAX2TG_TELEGRAM_TOPICS_ENABLED=true` и `MAX2TG_TELEGRAM_FORUM_CHAT_ID=-100…`
(супергруппа с Topics, бот — админ с «Manage Topics»). Полный список настроек
(темы/форум, исключения чатов, пейсинг API, глубина/источник предзагрузки, метод
входа, путь к SQLite-сессии на `/data`) — с комментариями в **`.env.example`**.

```bash
cp .env.example .env
nano .env        # заполните MAX2TG_* (см. комментарии в .env.example)
```

---

## Вариант A — своё всегда-включённое устройство (рекомендуется)

Домашний мини-ПК, старый ноут, Raspberry Pi или WSL на этом же ПК. **Бесплатно,
российский IP, без гео-проблем.**

### Linux / Raspberry Pi (systemd)

```bash
sudo mkdir -p /opt/max2tg && cd /opt/max2tg
# скопируйте сюда *.py, requirements.txt, .env
python3 -m venv venv
venv/bin/pip install -r requirements.txt
sudo cp max2tg.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now max2tg
journalctl -u max2tg -f          # смотреть логи
```

### Этот же ПК, но автозапуск (Windows)

`run.bat` уже умеет работать; для автостарта положите ярлык `run.bat` в
`shell:startup` (Win+R → `shell:startup`). Чтобы ПК не засыпал — отключите сон в
параметрах питания.

---

## Вариант B0 — Готовый Docker-образ из GHCR (проще всего, без исходников)

CI собирает образ на каждый push в `main` и публикует его в GitHub Container
Registry: **`ghcr.io/neuralgoose/max2tg:latest`**. Исходники качать не нужно — на
сервере достаточно двух файлов: `docker-compose.yml` и `.env`.

```bash
mkdir max2tg && cd max2tg
# забрать только compose-файл и шаблон env (без клонирования репозитория)
curl -O https://raw.githubusercontent.com/NeuralGoose/max2tg/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/NeuralGoose/max2tg/main/.env.example
nano .env                          # заполните MAX2TG_* (см. .env.example; свежий токен MAX!)
docker compose up -d               # сам подтянет ghcr.io/neuralgoose/max2tg:latest
docker compose logs -f
```
Обновление на свежий образ позже: `docker compose pull && docker compose up -d`.
Карта тем хранится на именованном томе `max2tg-data`, переживает обновления.

> Хотите собирать из локального кода вместо готового образа — используйте
> `docker compose -f docker-compose.build.yml up -d --build`.

### Preprod: перенос с dev (bind mount `./data`)

Локальная разработка (`docker-compose.build.yml`) хранит сессию и карту тем в
`max2tg/data/` на хосте (`state.json`, `max.db`). Эту папку можно скопировать на
preprod вместе с репозиторием — путь внутри контейнера остаётся `/data`.

**На dev (один раз, если ещё не сделано):**

```powershell
cd max2tg
docker compose -f docker-compose.build.yml down    # без -v
docker run --rm -v max2tg_max2tg-data:/from -v "${PWD}/data:/to" alpine sh -c "cp -a /from/. /to/"
```

**На preprod** (сборка из исходников):

```bash
# скопируйте монорепо (max2tg/ + PyMax/), .env и каталог data/
cd /opt/max2tg/max2tg
docker compose -f docker-compose.build.yml up -d --build
docker compose -f docker-compose.build.yml logs -f
```

После успешного запуска на preprod именованный том на dev можно удалить:
`docker volume rm max2tg_max2tg-data`.

> При смене IP MAX может потребовать повторный вход (QR/SMS), но `state.json`
> (темы) сохранится. Первый запуск на новом хосте — без `-d`, чтобы видеть логи.

---

## Вариант B — Oracle Cloud Always Free (Docker) — выбранный

Настоящая бесплатная VM 24/7 навсегда. Нужна карта при регистрации (без
списания). Регион зарубежный → возможно понадобится прокси для MAX (см. ниже).

### Шаг 1. Создать аккаунт и VM (делаете вы)
1. Регистрация: https://www.oracle.com/cloud/free/ → «Start for free».
   Регион (Home Region) выбирается один раз — берите ближайший (напр. Frankfurt).
2. Compute → Instances → **Create Instance**:
   - Image: **Ubuntu 22.04**.
   - Shape: **Ampere (ARM) VM.Standard.A1.Flex** — он входит в Always Free.
   - Добавьте/скачайте **SSH-ключ** (понадобится для входа).
3. Networking → разрешать ничего входящего не нужно (мост сам исходящий).
4. Запомните публичный IP инстанса.

### Шаг 2. Зайти на VM и положить файлы
```bash
ssh -i путь/к/ключу ubuntu@<IP_инстанса>
mkdir max2tg && cd max2tg
```
Перенесите файлы проекта на VM одним из способов:
- **scp** с вашего ПК (для сборки из исходников нужен `docker-compose.build.yml`):
  `scp -i ключ *.py requirements.txt Dockerfile docker-compose.build.yml server_setup.sh ubuntu@<IP>:~/max2tg/`
- либо `git clone` приватного репозитория (`.env`/`config.json` в .gitignore — не утекут).

### Шаг 3. Заполнить токены и запустить
```bash
cp .env.example .env
nano .env                      # заполните MAX2TG_* (см. .env.example; свежий токен MAX!)
chmod +x server_setup.sh
./server_setup.sh              # поставит Docker, соберёт и запустит
sudo docker compose logs -f
```
Ждём строку `Bridge online (own id: ...)`.

### Шаг 4. Если MAX не подключается (гео-блок IP)
В логах вместо «Bridge online» — ошибки соединения с `oneme.ru`. Тогда направьте
websocket MAX через прокси с российским IP:
```bash
echo 'MAX2TG_WS_PROXY=socks5://user:pass@ru-proxy-host:1080' >> .env
# для socks-прокси добавьте зависимость:
echo 'python-socks' >> requirements.txt
sudo docker compose -f docker-compose.build.yml up -d --build
```
(HTTP-прокси `http://host:3128` работает без доп. пакетов.)

---

## Проверка после запуска

1. В логах должно появиться `Bridge online (own id: ...)`.
2. Напишите себе в MAX с другого аккаунта — сообщение придёт в Telegram.
3. Ответьте через Reply — проверьте, что ушло обратно в MAX.

## Обновление токена

Если мост написал «токен MAX устарел» — обновите `MAX2TG_MAX_TOKEN` в `.env` и
перезапустите (`systemctl restart max2tg` / `docker compose restart`).
