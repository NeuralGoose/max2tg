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
не нужно. Возьмите значения:

- `MAX2TG_TELEGRAM_BOT_TOKEN` — токен бота от @BotFather.
- `MAX2TG_TELEGRAM_CHAT_ID` — ваш chat_id (есть в локальном `config.json`).
- `MAX2TG_MAX_TOKEN` — на [web.max.ru](https://web.max.ru): `F12` → Console →
  `copy(JSON.parse(localStorage.__oneme_auth).token)`.

```bash
cp .env.example .env
nano .env        # вставьте три значения
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
- **scp** с вашего ПК:
  `scp -i ключ *.py requirements.txt Dockerfile docker-compose.yml server_setup.sh ubuntu@<IP>:~/max2tg/`
- либо `git clone` приватного репозитория (`.env`/`config.json` в .gitignore — не утекут).

### Шаг 3. Заполнить токены и запустить
```bash
cp .env.example .env
nano .env                      # три значения MAX2TG_* (свежий токен MAX!)
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
sudo docker compose up -d --build
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
