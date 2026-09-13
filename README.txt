V380 Event — автономное Python-приложение без браузера

1. Установите Python 3.11 или новее на Windows x64.
2. Скопируйте .env.windows.example в .env.windows.
3. Заполните V380_CAMERA_ID и V380_PASSWORD.
4. При необходимости заполните Telegram и TeraBox параметры.
5. Положите необходимые EXE и TeraBox uploader в папки по схеме ниже.
6. Запустите start.bat — откроется графическая программа и сервис запустится автоматически.

Важно: V380_Event.py, папка bin и native-mediamtx.yml должны находиться в одной папке. Не запускайте старый файл из предыдущего архива. Новая версия автоматически понимает как простую раскладку bin\\V380Decoder.exe, bin\\mediamtx.exe, bin\\ffmpeg.exe, так и старую раскладку bin\\decoder\\, bin\\mediamtx\\, bin\\ffmpeg\\.

Структура папки:

V380_Event.py              главный Python-файл
start.bat                  запуск приложения
native-mediamtx.yml        конфигурация MediaMTX
.env.windows               ваша конфигурация, создать из example

bin\\
  V380Decoder.exe           декодер V380 Cloud
  mediamtx.exe              MediaMTX
  ffmpeg.exe                FFmpeg
  node.exe                  Node.js, нужен только для TeraBox uploader
  node_modules\\             зависимости TeraBox uploader, если они требуются

terabox\\app\\
  app-uploader.js            TeraBox uploader
  .config.yaml               создаётся программой автоматически
  остальные файлы uploader

 data\\
  events\\                   создаётся/используется для event-клипов
  continuous\\               создаётся/используется для 30-минутных сегментов
  logs\\                     логи программы и MediaMTX/decoder

Откуда взять EXE:

- V380Decoder.exe — Windows x64 release проекта V380Decoder.
- mediamtx.exe — Windows amd64 release MediaMTX.
- ffmpeg.exe — статическая Windows x64 сборка FFmpeg.
- node.exe — Node.js Windows x64 portable или установленный Node.js в PATH.
- TeraBox uploader — папка app и зависимости terabox-node из проверенной рабочей версии проекта.

Графическая программа:

Запустите `start.bat` или `py -3 V380_GUI.py`. Во вкладке «Настройки» можно изменить параметры камеры, Telegram, TeraBox, записи и движения. Кнопка «Сохранить настройки» записывает `.env.windows`.

При запуске графическая программа автоматически вызывает запуск сервиса, поэтому кнопку «Запустить сервис» нажимать не нужно. Во вкладке «Проверка системы» проверяются V380Decoder, MediaMTX, FFmpeg, данные камеры, Telegram и TeraBox. Переключатели во вкладке «Настройки» включают или отключают Motion Detector, Telegram, TeraBox-события, постоянную запись и загрузку continuous в TeraBox. Кнопки внизу запускают, останавливают и проверяют сервис.

Запуск вручную:

  start.bat

или:

  py V380_Event.py

Остановка: нажмите Ctrl+C в окне программы. Программа завершит FFmpeg, MediaMTX и V380Decoder.

Браузер, web UI, gateway и просмотр видео не используются. Программа только получает поток, обнаруживает движение, создаёт клипы, отправляет Telegram/TeraBox и пишет continuous-сегменты.

Если в логе появляется WinError 10060 при отправке Telegram, камера и motion detector при этом уже работают, но Windows не может подключиться к api.telegram.org. Укажите прокси в .env.windows, например:

  TELEGRAM_HTTPS_PROXY=http://127.0.0.1:10808

Программа также пытается автоматически использовать системный прокси Windows. После изменения .env.windows остановите программу Ctrl+C и запустите start.bat снова.

TeraBox: `TERABOX_NDUS` — это временная cookie активной сессии, а не пароль аккаунта. При проверке uploader отвечает `"ndus" cookie is BAD`, если cookie истекла, была скопирована не полностью или получена не из активной сессии TeraBox. В этом случае войдите в TeraBox в браузере, заново скопируйте полное значение cookie `ndus` и вставьте его в графическое поле TeraBox NDUS. Не добавляйте кавычки и не публикуйте это значение.
