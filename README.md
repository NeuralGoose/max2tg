# MAX → Telegram 🚀

<p align="center">
  <img src="assets/hero.svg" alt="max2tg — мост между мессенджером MAX и Telegram" width="820">
</p>

<p align="center">
  <a href="https://github.com/Sillkiin/max2tg/actions/workflows/ci-docker.yml"><img src="https://github.com/Sillkiin/max2tg/actions/workflows/ci-docker.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/Sillkiin/max2tg/pkgs/container/max2tg"><img src="https://img.shields.io/badge/ghcr.io-max2tg%3Alatest-2496ED?logo=docker&logoColor=white" alt="Docker image"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-3DA639" alt="MIT"></a>
  <img src="https://img.shields.io/badge/Windows%20·%20Android%20·%20Docker-informational" alt="Platforms">
</p>

<p align="center">
  <b>Читайте и отвечайте на сообщения мессенджера MAX (max.ru) прямо в Telegram.</b><br>
  Двусторонний · медиа · отдельная тема на каждый чат · бесплатно · self-hosted
  &nbsp;·&nbsp; 🇬🇧 <a href="#-in-english">In English</a>
</p>

---

`max2tg` — личный мост: зеркалит ваш аккаунт **MAX** в **Telegram**. Входящие
сообщения, фото, видео, файлы и стикеры прилетают в Telegram, а ответить можно
прямо оттуда — ответ уходит в нужный чат MAX.

> ## <img src="assets/apple.svg" height="22" align="top"> Для пользователей iPhone / iPad
>
> **MAX удалён из App Store** — на iOS его официально не установить, и нормально
> пользоваться MAX на Apple сейчас по сути невозможно. **`max2tg` это решает:**
> все ваши диалоги MAX приходят в **Telegram**, который у вас уже есть, —
> **с обычными push-уведомлениями** 🔔, поиском и нормальным клиентом. Отвечать
> тоже можно прямо из Telegram. По факту это **единственный рабочий способ**
> читать и писать в MAX с iPhone.

<p align="center">
  <img src="assets/shot-notification.svg" width="232" alt="Пуш-уведомление MAX в Telegram на экране iPhone">
  &nbsp;
  <img src="assets/shot-topics.svg" width="232" alt="Каждый чат MAX — отдельная тема в Telegram">
  &nbsp;
  <img src="assets/shot-reply.svg" width="232" alt="Ответ из Telegram уходит обратно в MAX">
</p>
<p align="center">
  <sub><b>1.</b> пуши из MAX на локскрине iPhone&nbsp;·&nbsp;<b>2.</b> каждый чат — отдельная тема&nbsp;·&nbsp;<b>3.</b> ответ из Telegram уходит обратно в MAX&nbsp;&nbsp;<i>(макеты)</i></sub>
</p>
<p align="center">
  <sub>Видно, что сообщения именно из MAX: мост помечает каждое — <code>MAX | Имя (чат …)</code>, голосовые как <code>🎤 Голосовое (N с) — открыть в MAX</code>, а доставку ответа подтверждает <code>✅ Отправлено в MAX</code>. Это реальный формат вывода моста.</sub>
</p>

## Содержание

- [Возможности](#возможности)
- [Быстрый старт](#быстрый-старт)
- [⭐ Режим тем](#-режим-тем)
- [Запуск на сервере 24/7](#запуск-на-сервере-247)
- [Вопросы и ответы](#вопросы-и-ответы)
- [Ограничения](#ограничения)
- [Для разработчиков](#для-разработчиков)

---

## Возможности

| | |
|---|---|
| <img src="assets/apple.svg" height="15"> **Работает на iOS** | MAX удалён из App Store — а здесь все диалоги приходят в Telegram, который на iPhone есть всегда |
| 🔔 **Уведомления** | Пуши о новых сообщениях MAX приходят как обычные уведомления Telegram — на iOS это единственный способ их получать |
| ↔️ **Двусторонний** | Не просто пересылка — отвечаете **из Telegram**, и сообщение уходит в чат MAX |
| 🗂 **Темы** | Каждый MAX-чат = отдельная тема Telegram-форума с именем собеседника |
| 🖼 **Медиа** | Фото, видео, файлы, стикеры — в обе стороны |
| 🔒 **Приватно** | Токены и переписка остаются у вас; ничего не уходит на чужие серверы |
| 🆓 **Бесплатно** | Без подписок, открытый код (MIT) |
| 🖥 **Где угодно** | Windows, старый Android (Termux) или сервер 24/7 |

**Что именно передаётся:**

| Направление | Содержимое |
|---|---|
| **MAX → Telegram** | текст, фото, видео, файлы, стикеры, пометки о голосовых |
| **Telegram → MAX** | текст (ответом), фото, видео, файлы |

---

## Быстрый старт

**Windows, ~5 минут.** Запустите **`run.bat`** — мастер настройки попросит три
вещи:

**1. Токен Telegram-бота**
Создайте бота у [@BotFather](https://t.me/BotFather) командой `/newbot` и
скопируйте выданный токен.

**2. `/start` вашему боту**
Напишите боту `/start`, чтобы мост узнал, куда слать сообщения.

**3. Токен MAX**
Войдите на [web.max.ru](https://web.max.ru), нажмите `F12` → вкладка **Console**,
выполните команду и вставьте результат в мастер:

```js
copy(JSON.parse(localStorage.__oneme_auth).token)
```

> ⚠️ **Не нажимайте «Выйти» (Logout)** в web.max.ru — это аннулирует токен.
> Просто закройте вкладку. Токен хранится локально в `config.json`.

Готово ✅ Это **базовый режим**: все сообщения MAX приходят в одну личку с ботом.
Дальше советуем включить [режим тем](#-режим-тем) — так гораздо удобнее.

---

## ⭐ Режим тем

Чтобы каждый MAX-чат стал **отдельной темой** Telegram-форума, а не сваливался в
одну ленту:

1. Создайте **Telegram-супергруппу** и включите в настройках **«Темы» (Topics)**.
2. **Добавьте бота в группу**, дайте права **администратора** с разрешением
   **«Управление темами» (Manage Topics)** — без этого мост не создаст темы.
3. Узнайте **id группы** (начинается с `-100…`, например через
   [@getidsbot](https://t.me/getidsbot)) и пропишите в `config.json`:

```json
{
  "telegram_topics_enabled": true,
  "telegram_forum_chat_id": -1001234567890,
  "telegram_preload_topics": true,
  "telegram_seed_last_messages": true,
  "telegram_confirm_sent": false
}
```

Каждый MAX-чат получит свою тему с именем собеседника. Пишете в теме — уходит в
этот чат MAX; **Reply** (свайп) отправляет ответ цитатой.

---

## Запуск на сервере 24/7

Мост держит постоянное соединение, поэтому ему нужен хост, который **не
«засыпает»** (обычные бесплатные «спящие» PaaS не подойдут).

| Где | Как |
|---|---|
| 🖥 **Windows** | `run.bat` + автозапуск через `shell:startup` |
| 📟 **Старый Android** | [Termux](https://f-droid.org/packages/com.termux/) — мини-сервер с российским IP |
| ☁️ **Сервер (Docker)** | готовый образ из GHCR, без исходников ↓ |

**Готовый Docker-образ — нужны только два файла:**

```bash
mkdir max2tg && cd max2tg
curl -O https://raw.githubusercontent.com/Sillkiin/max2tg/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/Sillkiin/max2tg/main/.env.example
nano .env                 # три значения MAX2TG_* (свежий токен MAX!)
docker compose up -d      # подтянет ghcr.io/sillkiin/max2tg:latest
```

Обновление позже: `docker compose pull && docker compose up -d`.
Полный гайд (Oracle Cloud, прокси, systemd) — в **[DEPLOY.md](DEPLOY.md)**.

---

## Вопросы и ответы

<details>
<summary><b>Темы пересоздаются после каждого перезапуска</b></summary>

<br>Значит не сохраняется `state.json` (карта «MAX-чат → тема»). В Docker он лежит
на постоянном томе (`docker-compose.yml`); путь можно задать через
`MAX2TG_STATE_PATH`. Без Docker файл хранится рядом со скриптами.
</details>

<details>
<summary><b>Как убрать «✅ Отправлено в MAX» после каждого ответа</b></summary>

<br>Добавьте в `config.json`: <code>"telegram_confirm_sent": false</code> (или
переменную окружения <code>MAX2TG_TELEGRAM_CONFIRM_SENT=false</code>). Ошибки
отправки при этом всё равно показываются.
</details>

<details>
<summary><b>Мост пишет, что токен MAX устарел</b></summary>

<br>Получите свежий токен на <a href="https://web.max.ru">web.max.ru</a> (та же
команда в консоли) и обновите его в <code>config.json</code> или в
<code>.env</code>, затем перезапустите мост.
</details>

---

## Ограничения

- ⚙️ **Неофициальный API MAX** (через [vkmax](https://github.com/nsdkinx/vkmax) —
  официального API для личных аккаунтов нет). Для MAX это выглядит как вход через
  веб-версию; теоретически он может ограничить сессию.
- 🎤 **Голосовые из MAX** приходят подписью «🎤 Голосовое (N с)» — само аудио
  недоступно даже веб-клиенту MAX.
- 📦 **Файлы и видео до ~50 МБ** (лимит Telegram-ботов); крупнее — уведомление
  «открыть в MAX».
- 🔑 **Токены** лежат локально в `config.json` (в `.gitignore`, не коммитятся).

---

## Для разработчиков

<details>
<summary>Структура проекта и сборка</summary>

<br>

| Файл | Назначение |
|------|------------|
| `main.py` / `setup_wizard.py` | точка входа и мастер настройки |
| `bridge.py` | ядро: слушает MAX, маршрутизирует, принимает ответы |
| `max_client.py` | WebSocket-клиент MAX (браузерные заголовки, вход по токену) |
| `attaches.py` / `mediamax.py` | разбор и загрузка медиа MAX |
| `tg.py` | мини-клиент Telegram Bot API |
| `state.py` / `config.py` | карта тем и конфигурация |
| `Dockerfile` / `docker-compose*.yml` / `DEPLOY.md` | деплой |

```bash
python -m unittest discover -s tests     # тесты
```

CI (GitHub Actions) на каждый push в `main` гоняет тесты и публикует
Docker-образ `ghcr.io/sillkiin/max2tg:latest`. Для локальной сборки из исходников:
`docker compose -f docker-compose.build.yml up -d --build`.
</details>

---

## 🇬🇧 In English

<details>
<summary>Click to expand</summary>

<br>**max2tg** mirrors your personal **MAX** (max.ru) messenger account into
**Telegram** and lets you reply from there.

> <img src="assets/apple.svg" height="15"> **MAX is gone from the App Store** — you can't install it on iPhone/iPad
> anymore. max2tg brings every MAX chat into **Telegram** (which you already
> have) **with normal push notifications** — effectively the only way to use MAX
> on iOS.

- **Two-way.** Incoming MAX messages — text, photos, videos, files, stickers —
  are forwarded to Telegram; reply right from Telegram and it lands in the MAX chat.
- **Topics.** Each MAX chat becomes its own Telegram forum topic, named after the contact.
- **Private & free.** Tokens and messages stay on your machine. Open-source (MIT).
- **Runs anywhere.** Windows, old Android (Termux), or a 24/7 server. Pull the
  ready image: `docker pull ghcr.io/sillkiin/max2tg:latest`.

**Quick start:** run `run.bat`, create a bot via [@BotFather](https://t.me/BotFather),
and paste your MAX token from [web.max.ru](https://web.max.ru) (DevTools console:
`copy(JSON.parse(localStorage.__oneme_auth).token)`).

> ⚠️ Uses MAX's **unofficial** web API. Voice messages from MAX are only labeled —
> MAX's own web client can't play them either. Use at your own risk.
</details>

---

## Дисклеймер

Личный инструмент для удобства, не связан с MAX/VK и Telegram. Использует
неофициальный API — применяйте на свой риск и соблюдайте условия сервисов.
**Лицензия: [MIT](LICENSE).**
