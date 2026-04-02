# TrackRip — Music Downloader

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Flask-Backend-black?logo=flask" alt="Flask">
  <img src="https://img.shields.io/badge/yt--dlp-Downloader-red?logo=youtube" alt="yt-dlp">
  <img src="https://img.shields.io/badge/Soulseek-slskd-green" alt="Soulseek">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/License-Private-orange" alt="License">
</p>
<p align="center">
  <img src="https://eblo.id/uploads/RW4Do3K/image.opt.webp">
</p>

<p align="center">
  <b>Веб-панель для массовой загрузки музыки с YouTube Music, YouTube, SoundCloud и Soulseek.</b><br>
  Автоматический поиск, параллельная загрузка, встроенный спидтест, импорт плейлистов.
</p>

---

## Описание

TrackRip — локальная веб-панель для загрузки музыки. Вы добавляете треки в формате `Автор - Название`, а панель автоматически ищет и скачивает их из нескольких источников, выбирая лучший доступный вариант.

Панель работает как локальный веб-сервер — открывается в браузере по адресу `http://localhost:8844`.

Так же есть готовый вариант на сайте `http://trackrip.30x.ru`.

> [!warning]
> Внимание, в веб версии есть ограничение в 400 треков за раз.
> В локальной версии ограничений нет. 

Список песен можно взять с помощью сайта https://ymusicexport.ru/
1. Открываете свой плейлист в Яндекс Музыке(в пк версии: заходите в плейлист -> три точки -> Приватный плейлист выкл).
2. Копируете ссылку(или HTML iframe), вставляете в ymusicexport, там получаете список `Автор - Название`.
3. Потом вставляете этот список в парсер(или в .txt для импорта).
4. Нажимаете старт и дождитесь завершения скачивания.

### Как это работает

1. Вы вводите `Queen - Bohemian Rhapsody` (или загружаете список из файла)
2. Панель параллельно ищет трек в **YouTube Music → YouTube → SoundCloud**
3. Если подключен Soulseek — одновременно ищет там (FLAC, lossless)
4. Скачивает лучший найденный вариант в выбранную папку

---

## Возможности

| Функция | Описание |
|---|---|
| 🎵 **4 источника** | YouTube Music, YouTube, SoundCloud, Soulseek (slskd) |
| ⚡ **Адаптивные воркеры** | 2–10 параллельных загрузок, подстраиваются под скорость канала |
| 📂 **Импорт плейлиста** | Загрузка из файла `Песни.txt` (формат: `Артист - Название`) |
| 📁 **Выбор папки загрузок** | Меняется прямо из интерфейса, сохраняется между перезапусками |
| 🏎 **Спидтест** | Встроенная страница мониторинга скорости загрузки |
| ▶ ⏸ ⏹ **Управление очередью** | Старт, пауза, стоп, повтор ошибок, очистка |
| 🔄 **Без конвертации** | Скачивается оригинальное аудио (m4a/opus/flac) — максимальная скорость |


---

## Быстрый старт (Windows)

1. Установите [Python 3.10+](https://www.python.org/downloads/) — обязательно отметьте **«Add Python to PATH»**
2. Распакуйте архив `MDL.zip`
3. Запустите **`run.bat`**
4. Откройте в браузере: **http://localhost:8844**

При первом запуске автоматически:
- Создаётся виртуальное окружение
- Устанавливаются зависимости (Flask, yt-dlp)
- Генерируется конфиг для slskd (если есть)

---

## Быстрый старт (Linux)

```bash
pip install flask yt-dlp
python -u server.py
```

---

## Soulseek (опционально)

Для загрузки с Soulseek нужен [slskd](https://github.com/slskd/slskd) — запущенный на `localhost:5030`.

При наличии `slskd.exe` в PATH или в папке `slskd/` — `run.bat` запустит его автоматически с конфигом `slskd.yml`.

Отредактируйте `slskd.yml` — укажите логин и пароль от Soulseek:

```yaml
soulseek:
  username: ВАШ_ЛОГИН
  password: ВАШ_ПАРОЛЬ
```

Без slskd панель работает полноценно через YouTube Music, YouTube и SoundCloud.

---

## Структура проекта

```
MDL/
├── server.py            # Бэкенд — Flask API + воркеры загрузки
├── static/
│   ├── index.html       # Главная страница
│   ├── style.css        # Стили (cyberpunk UI)
│   ├── app.js           # Фронтенд-логика
│   └── speedtest.html   # Страница спидтеста
├── example.txt    # Пример плейлиста для импорта
├── requirements.txt     # Python-зависимости
├── run.bat              # Скрипт запуска для Windows
├── slskd.yml            # Конфиг Soulseek (создаётся автоматически)
└── .config.json         # Сохранённые настройки (создаётся автоматически)
```

---

## Требования

| Компонент | Версия |
|---|---|
| Python | 3.10 и выше |
| yt-dlp | последняя (устанавливается автоматически) |
| Flask | последняя (устанавливается автоматически) |
| slskd | 0.24.5+ (опционально, для Soulseek) |
| ОС | Windows 10/11, Linux (Ubuntu 20.04+, Debian 12+) |

---
