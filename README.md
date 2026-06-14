# MAX → Telegram 🚀

<p align="center">
  <img src="assets/hero.svg" alt="max2tg — мост между мессенджером MAX и Telegram" width="820">
</p>

> 🇬🇧 *Mirror your MAX (max.ru) messenger into Telegram — read & reply from Telegram. Two-way, media, per-chat topics. Free & self-hosted.* → [In English](#-in-english)

**Читайте и отвечайте на сообщения мессенджера MAX (max.ru) прямо в Telegram.**

`max2tg` — личный мост, который зеркалит ваш аккаунт **MAX** в **Telegram**:
входящие сообщения, фото, видео, файлы, стикеры прилетают в удобный Telegram, а
ответить можно прямо оттуда. Бесплатно, с открытым кодом, работает на вашем ПК,
старом Android или сервере.

> 📱 **Особенно удобно на iOS.** Не хотите держать ещё одно приложение? Все
> диалоги MAX оказываются в Telegram — там, где вы и так читаете сообщения.
> Уведомления, поиск, нормальный клиент — всё привычное.

---

## Зачем это нужно

- 💬 **Один мессенджер вместо двух.** MAX появляется у всё большего числа людей в
  СНГ — но читать и отвечать удобнее в Telegram. Мост сводит всё в одно место.
- ↔️ **Двусторонний.** Это не просто пересылка — вы **отвечаете из Telegram**, и
  сообщение уходит в нужный чат MAX.
- 🗂 **Каждый чат MAX — отдельная тема** в Telegram-форуме. Диалоги не
  смешиваются, всё подписано реальными именами.
- 🖼 **Медиа в обе стороны:** фото, видео, файлы, стикеры.
- 🔒 **Приватно.** Токены и переписка остаются у вас — ничего не уходит на чужие
  серверы. Только ваш бот, ваш аккаунт, ваша машина.
- 🆓 **Бесплатно** и без подписок.

## Что умеет

| Направление | Что передаётся |
|---|---|
| **MAX → Telegram** | текст, фото, видео, файлы, стикеры, пометки о голосовых |
| **Telegram → MAX** | текст (ответом), а также фото / видео / файлы |
| **Темы** | каждый MAX-чат = тема Telegram-форума с именем собеседника |

---

## 🇬🇧 In English

**max2tg** mirrors your personal **MAX** (max.ru) messenger account into
**Telegram** and lets you reply from there — handy if you'd rather not keep yet
another app (especially on **iOS**).

- **Two-way.** Incoming MAX messages — text, photos, videos, files, stickers —
  are forwarded to Telegram; reply right from Telegram and it lands in the MAX
  chat.
- **Topics.** Each MAX chat becomes its own Telegram forum topic, named after
  the contact.
- **Private & free.** Tokens and messages stay on your machine — nothing goes to
  third-party servers. Open-source (MIT).
- **Runs anywhere.** Windows, old Android (Termux), or a 24/7 server (Docker /
  systemd — see [DEPLOY.md](DEPLOY.md)).

**Quick start:** run `run.bat`, create a bot via [@BotFather](https://t.me/BotFather),
and paste your MAX token from [web.max.ru](https://web.max.ru) (DevTools console:
`copy(JSON.parse(localStorage.__oneme_auth).token)`).

> ⚠️ Uses MAX's **unofficial** web API (no official personal API exists). Voice
> messages from MAX are only labeled — MAX's own web client can't play them
> either. Use at your own risk.

---

## Быстрый старт (Windows, ~5 минут)

Запустите `run.bat` — мастер настройки попросит три вещи:

1. **Токен Telegram-бота** — создайте бота у [@BotFather](https://t.me/BotFather)
   командой `/newbot` (бесплатно).
2. **`/start` вашему боту** — чтобы мост знал, куда слать сообщения.
3. **Токен MAX** — войдите на [web.max.ru](https://web.max.ru), нажмите `F12` →
   вкладка **Console**, выполните и вставьте результат в мастер:
   ```js
   copy(JSON.parse(localStorage.__oneme_auth).token)
   ```
   > Токен сохраняется локально. **Не нажимайте Logout** в web.max.ru — это его
   > аннулирует; просто закройте вкладку.

Готово — входящие сообщения MAX начнут приходить в Telegram.

## Где запускать

- 🖥 **Windows** — `run.bat` (+ автозапуск через `shell:startup`).
- 📟 **Старый Android** — через [Termux](https://f-droid.org/packages/com.termux/)
  (мини-сервер 24/7, российский IP).
- ☁️ **Сервер 24/7** — Docker или systemd, см. **[DEPLOY.md](DEPLOY.md)**. Токены
  задаются через `.env`, браузер не нужен.

## Темы = диалоги

Создайте Telegram-супергруппу, включите **Темы**, добавьте бота админом с правом
управлять темами и пропишите в `config.json`:

```json
{
  "telegram_topics_enabled": true,
  "telegram_forum_chat_id": -1001234567890,
  "telegram_preload_topics": true,
  "telegram_seed_last_messages": true
}
```

Каждый MAX-чат получит свою тему с именем собеседника (связь хранится в
`state.json`). Пишете в теме — уходит в этот чат MAX; `Reply` отправляет ответ
цитатой.

> Чтобы бот **не писал** «✅ Отправлено в MAX» после каждого ответа (не засорять
> тему), добавьте в `config.json`: `"telegram_confirm_sent": false`. Ошибки
> отправки при этом всё равно показываются.

---

## Честные ограничения

- ⚙️ Используется **неофициальный** API MAX (через
  [vkmax](https://github.com/nsdkinx/vkmax) — официального API для личных
  аккаунтов нет). Для MAX сессия выглядит как вход через веб-версию; теоретически
  он может её ограничить.
- 🎤 **Голосовые из MAX** приходят подписью «🎤 Голосовое (N с)» — само аудио
  недоступно даже веб-клиенту MAX (он помечает их `UNSUPPORTED`).
- 📦 Файлы/видео до **~50 МБ** (лимит Telegram-ботов); что больше — уведомление
  «открыть в MAX».
- 🔑 Токены лежат локально в `config.json` (в `.gitignore`, не коммитятся).

## Как это устроено

| Файл | Назначение |
|------|------------|
| `main.py` / `setup_wizard.py` | точка входа и мастер настройки |
| `bridge.py` | ядро: слушает MAX, маршрутизирует, принимает ответы |
| `max_client.py` | WebSocket-клиент MAX (браузерные заголовки, вход по токену) |
| `attaches.py` / `mediamax.py` | разбор и загрузка медиа MAX |
| `tg.py` | мини-клиент Telegram Bot API |
| `state.py` / `config.py` | карта тем и конфигурация |
| `Dockerfile` / `max2tg.service` / `DEPLOY.md` | деплой на сервер |

## Дисклеймер

Личный инструмент для удобства, не связан с MAX/VK и Telegram. Использует
неофициальный API — применяйте на свой риск и соблюдайте условия сервисов.
Лицензия: MIT.
