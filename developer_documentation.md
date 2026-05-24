# Документация разработчика

## 1. Назначение документа

Этот документ описывает текущее состояние проекта ONVIF PTZ Controller для разработчиков:

- спецификацию этапов MVP и MUP
- актуальную архитектуру приложения
- внутренний API модулей
- схему ONVIF-интеграции
- фактический результат проекта на текущем этапе

Документ отражает состояние репозитория в его текущем виде, а не только исходный план из раннего прототипа.

## 2. Спецификация MVP / MUP

### 2.1 Целевой MVP по исходному плану

Согласно исходной спецификации, этап MVP должен был включать:

| Требование | Плановый статус MVP | Фактическое состояние в текущем репозитории |
|---|---|---|
| Ручное добавление камер | Обязательно | Реализовано |
| Автообнаружение камер по сети | Обязательно | Реализовано через WS-Discovery (`camera_discovery.py`) |
| Полное PTZ-управление | Обязательно | Реализовано |
| Работа с пресетами | Обязательно | Реализовано в основном сценарии: просмотр, переход, создание, удаление |
| Отображение видео | Обязательно | Реализовано |
| Мультиплатформенность | Обязательно | Реализовано на уровне исходников; Windows standalone проверен; Linux/macOS требуют native build |
| Документация пользователя и разработчика | Обязательно | Реализовано этим комплектом документов |
| Отзыв пользователя | Обязательно | В кодовой базе отсутствует как отдельный артефакт |

### 2.2 Целевой MUP по исходному плану

Следующий этап MUP из исходной спецификации включает:

- внедрение в реальную тестовую среду
- самостоятельное использование пользователями
- сбор отзыва по реальному сценарию эксплуатации
- обновление документации на основе реального фидбэка
- упаковку и подготовку материалов для распространения

### 2.3 Фактический результат относительно MVP

По программной части проект уже вышел за рамки базового MVP и включает не только обязательные функции этапа, но и дополнительные возможности:

- работа с несколькими камерами одновременно
- сохранённая локальная библиотека камер
- автообнаружение ONVIF-камер в локальной сети через WS-Discovery (мультикаст и unicast)
- browser-style вкладки камер
- Matrix mode для одновременного просмотра нескольких камер
- воспроизведение RTSP-аудио с регулировкой громкости
- редакторы Video Streaming, Imaging Settings и Profiles
- разделы Device Management: Network Settings, Maintenance, User Management
- standalone build для Windows и подготовленный пайплайн сборки для Linux/macOS

Из формальных пунктов исходной спецификации остаётся незакрытым только один:

- отдельный оформленный отзыв конечного пользователя

## 3. Актуальная архитектура

### 3.1 Общая схема

```text
┌─────────────────────────────────────────────────────────────┐
│                    PyQt6 Desktop UI                        │
│                        main_window.py                      │
│                                                             │
│  Left rail        Center workspace         Right rail       │
│  Add Camera       Single tabs / Matrix     Info / PTZ       │
│  Saved Cameras    Active RTSP video         STR / IMG        │
│                                          Profiles / NET      │
│                                          Maintenance / Users │
└──────────────────────────────┬──────────────────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            │                                     │
┌───────────▼───────────┐             ┌──────────▼───────────┐
│ ONVIF SOAP layer      │             │ RTSP playback layer  │
│ onvif_client.py       │             │ video_player.py      │
│ Device / Media / PTZ  │             │ PyAV + FFmpeg        │
│ Imaging / DeviceMgmt  │             │ UDP first, TCP fallback |
└───────────┬───────────┘             └──────────┬───────────┘
            │                                    │
            └────────────────┬───────────────────┘
                             │
                   ┌─────────▼─────────┐
                   │ ONVIF camera       │
                   │ SOAP + RTSP        │
                   └────────────────────┘
```

### 3.2 Основные модули

| Файл | Роль |
|---|---|
| `main.py` | Точка входа, логирование, создание `QApplication` и `MainWindow` |
| `main_window.py` | Основная логика UI, работа с вкладками, Matrix mode, управлением камерами и всеми inspector-панелями |
| `onvif_client.py` | Асинхронный ONVIF-клиент поверх `onvif-zeep-async` |
| `camera_discovery.py` | WS-Discovery: отправка Probe-запросов, разбор ProbeMatch-ответов, определение подсетей для автопоиска |
| `video_player.py` | Захват и декодирование RTSP-видео через PyAV; воспроизведение аудио через sounddevice |
| `build_standalone.py` | Скрипт standalone-сборки через PyInstaller |
| `onvif_ptz_controller.spec` | PyInstaller spec для standalone bundle |

### 3.3 Ключевые сущности

| Сущность | Где находится | Назначение |
|---|---|---|
| `CameraSession` | `main_window.py` | Состояние одной активной камеры: UI-страница, RTSP URI, loop, ONVIF client, media profiles, Matrix tile |
| `SavedCameraConfig` | `main_window.py` | Локально сохранённая конфигурация камеры |
| `MediaProfileInfo` | `onvif_client.py` | Упрощённое представление ONVIF media profile |
| `ImagingSettingsPayload` | `onvif_client.py` | Сериализованные imaging settings и options для динамического UI |
| `NetworkSettingsPayload` | `onvif_client.py` | Снимок Device Management network settings |
| `UserAccountInfo` | `onvif_client.py` | Строка пользователя камеры для User Management |

### 3.4 Потоки выполнения

Проект использует комбинированную модель:

- GUI-поток PyQt6 для интерфейса
- отдельный `QThread` (`VideoStreamThread`) на каждую камеру — супервизор, читающий очередь от дочернего процесса
- отдельный `multiprocessing.Process` на каждую камеру — изолированный FFmpeg-процесс с декодированием видео и воспроизведением аудио через sounddevice; изоляция защищает GUI от C-level краша в кодеке
- отдельный `asyncio` loop на каждую подключённую камеру для ONVIF-запросов
- `QThread` (`_CameraDiscoveryWorker`) для WS-Discovery — чтобы сканирование сети не блокировало GUI

Изоляция FFmpeg/sounddevice в дочернем процессе позволяет:
- остановить аудио гарантированно, завершив процесс
- не потерять GUI при segfault в нативном кодеке
- регулировать громкость через `multiprocessing.Queue` без общей памяти между потоками

### 3.5 Локальные данные

Сохранённые камеры хранятся в `saved_cameras.json` в `QStandardPaths.AppConfigLocation`.

Если системный путь недоступен, используется резервная директория `.onvif-ptz-controller` в текущей рабочей папке.

## 4. Внутренний API

### 4.1 `main.py`

Основной публичный вход:

- `setup_logging()`
- `main()`

Ответственность модуля ограничена запуском приложения. Бизнес-логика внутри него не хранится.

### 4.2 `main_window.py`

`MainWindow` является orchestration-layer между UI, видеопотоком и ONVIF-клиентом.

Ключевые группы методов:

| Группа | Примеры методов | Назначение |
|---|---|---|
| Управление камерами | `_on_connect`, `_connect_camera_config`, `_remove_camera_session` | Создание и удаление сессий камер |
| Обновление UI | `_refresh_active_camera_ui`, `_show_empty_state` | Перерисовка инспектора и рабочего пространства |
| Matrix mode | `_move_session_to_matrix`, `_rebuild_matrix_grid`, `_on_matrix_tile_clicked` | Управление множественным просмотром |
| PTZ | `_on_ptz_move`, `_on_ptz_stop` | Проксирование команд в ONVIF client |
| Presets | `_on_refresh_presets`, `_on_goto_preset`, `_on_save_preset`, `_on_delete_preset` | Работа с пресетами |
| Media / Imaging | `_on_refresh_video_streaming`, `_on_apply_video_streaming`, `_on_refresh_imaging_settings`, `_on_apply_imaging_settings` | Управление encoder и imaging параметрами |
| Device Management | `_on_refresh_network_settings`, `_on_apply_network_settings`, `_on_reboot_device`, `_on_refresh_user_accounts` | Сетевые и административные ONVIF-операции |
| Горячие клавиши | `_handle_global_key_event`, `_perform_ptz_action` | Глобальные shortcuts и PTZ hotkeys |

### 4.3 `onvif_client.py`

`ONVIFPTZClient` инкапсулирует все прямые обращения к ONVIF services.

Ключевые методы по категориям:

| Категория | Методы |
|---|---|
| Подключение | `connect`, `disconnect`, `refresh_media_profiles`, `get_media_profiles` |
| PTZ | `continuous_move`, `relative_move`, `absolute_move`, `stop_move`, `get_status` |
| Presets | `get_presets`, `goto_preset`, `save_preset`, `delete_preset` |
| Media | `get_stream_uri`, `get_video_encoder_settings`, `set_video_encoder_settings`, `create_profile`, `edit_profile`, `delete_profile` |
| Imaging | `get_imaging_settings`, `set_imaging_settings` |
| Device Management | `get_network_settings`, `set_network_settings`, `get_user_accounts`, `create_user_account`, `update_user_account`, `delete_user_account`, `system_reboot`, `system_factory_reset`, `upgrade_firmware` |

### 4.4 `video_player.py`

Основной runtime API `VideoStreamThread`:

| Метод | Назначение |
|---|---|
| `set_url(url)` | Устанавливает RTSP URL до запуска потока |
| `set_volume(v)` | Отправляет команду `('vol', v)` в `cmd_q` дочернего процесса; значение применяется немедленно — при следующем аудиокадре |
| `start()` / `stop_stream()` | Запуск/остановка: `stop_stream()` устанавливает `stop_event`, что будит watcher-поток внутри subprocess, вызывающий `container.close()` для немедленного выхода из `demux()` |

Сигналы: `frame_ready(QImage)`, `stream_started()`, `stream_stopped()`, `error_occurred(str)`.

Логика `_rtsp_subprocess_worker`:

- сначала пробует UDP, при неудаче — TCP (чередуется по номеру попытки)
- не считает успехом «открытие контейнера без кадров»
- ограничивает частоту отправки кадров в очередь (≤ 30 fps), чтобы снизить задержку
- громкость хранится в локальной переменной `current_vol`; обновляется через `cmd_q.get_nowait()` перед каждым аудиокадром
- при намеренной остановке (`stop_event.is_set()`) не запускает повторное подключение

## 5. Обнаружение камер (WS-Discovery)

### 5.1 Принцип работы

`camera_discovery.py` реализует поиск ONVIF-устройств через протокол WS-Discovery (SOAP over UDP).

Два режима:

- **Мультикаст** — Probe-пакет отправляется на `239.255.255.250:3702`; поддерживающие WS-Discovery камеры отвечают ProbeMatch; используется когда поле `Targets` пустое и нет подходящих локальных интерфейсов
- **Unicast** — Probe-пакет отправляется напрямую на каждый адрес из сформированного списка; ответ ожидается от конкретного хоста; используется всегда при наличии локальных подсетей или явного поля `Targets`

### 5.2 Определение адресов для поиска

При пустом поле `Targets` приложение определяет локальные подсети автоматически:

- **Windows**: через PowerShell `Get-NetIPAddress`; виртуальные интерфейсы (VMware, Hyper-V, Docker, WSL, Tailscale, WireGuard и др.) исключаются
- **Другие ОС**: через `socket.getaddrinfo(hostname)`
- Если реальных интерфейсов не найдено, используется fallback с prefixlen `/24`
- Prefix короче `/24` принудительно ограничивается до `/24`

### 5.3 Разбор ProbeMatch

Из ответа извлекается:

- `XAddrs` — список URL ONVIF device service; предпочтение отдаётся адресу, совпадающему с IP отправителя
- `Scopes` — ONVIF-scopes: `name`, `hardware`, `location`
- `Types` — определение `NetworkVideoTransmitter` используется как признак камеры

Дубликаты (одинаковый `host:port`) сливаются с объединением полей.

### 5.4 Публичный API `camera_discovery.py`

| Функция | Назначение |
|---|---|
| `discover_onvif_cameras(timeout, targets)` | Основная точка входа: отправляет Probe, собирает ProbeMatch, возвращает список `DiscoveredCameraInfo` |
| `parse_discovery_targets(text)` | Разбирает строку фильтра в список IP-адресов; поддерживает одиночные IP, частичные октеты (`192.168`), CIDR (`192.168.1.0/24`), списки через запятую/пробел |
| `default_discovery_targets(preferred_host)` | Возвращает список адресов из локальных подсетей для автоматического поиска |
| `local_discovery_networks()` | Возвращает список `IPv4Network` физических интерфейсов машины |

`DiscoveredCameraInfo` — датакласс с полями `host`, `port`, `xaddr`, `scopes`, `types` и вычисляемыми свойствами `name`, `hardware`, `location`, `is_camera`.

## 6. ONVIF-интеграция

### 6.1 Используемые ONVIF-сервисы

При подключении приложение пытается создать следующие сервисы:

| Сервис | Статус |
|---|---|
| Device Management | Обязательный |
| Media | Обязательный |
| PTZ | Опциональный |
| Imaging | Опциональный |

Если PTZ или Imaging не поддерживаются камерой, приложение не падает: соответствующие разделы скрываются или переходят в read-only/empty state.

### 6.2 Основные ONVIF-операции

#### Device Management

- `GetDeviceInformation`
- `GetNetworkInterfaces`, `SetNetworkInterfaces`
- `GetNetworkDefaultGateway`, `SetNetworkDefaultGateway`
- `GetHostname`, `SetHostname`, `SetHostnameFromDHCP`
- `GetDNS`, `SetDNS`
- `GetNTP`, `SetNTP`
- `GetNetworkProtocols`, `SetNetworkProtocols`
- `GetZeroConfiguration`, `SetZeroConfiguration`
- `GetDiscoveryMode`, `SetDiscoveryMode`
- `GetUsers`, `CreateUsers`, `SetUser`, `DeleteUsers`
- `SystemReboot`, `SetSystemFactoryDefault`
- `StartFirmwareUpgrade`, `UpgradeSystemFirmware`

#### Media

- `GetProfiles`
- `GetStreamUri`
- `GetVideoEncoderConfiguration`
- `GetVideoEncoderConfigurationOptions`
- `SetVideoEncoderConfiguration`
- `CreateProfile`
- `DeleteProfile`

#### PTZ

- `ContinuousMove`
- `RelativeMove`
- `AbsoluteMove`
- `Stop`
- `GetStatus`
- `GetPresets`
- `GotoPreset`
- `SetPreset`
- `RemovePreset`

#### Imaging

- `GetOptions`
- `GetImagingSettings`
- `SetImagingSettings`

### 6.3 Особенности интеграции

- WSDL-файлы берутся из установленного пакета `onvif`
- часть операций зависит от конкретного вендора и прошивки камеры
- Device Management UI работает в best-effort режиме: приложение пытается применить доступные поля по частям, а не считает всю секцию недоступной при одном несовместимом параметре
- для `ZeroConfiguration` используется фактическая сигнатура ONVIF `GetZeroConfiguration()` без аргументов
- для hostname DHCP/manual используется отдельная операция `SetHostnameFromDHCP`

## 7. Сборка и выпуск

### 7.1 Сборка из исходников

```bash
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate     # Windows
pip install -r requirements.txt
python main.py
```

### 7.2 Standalone build

```bash
pip install -r requirements-build.txt
python build_standalone.py
```

Результат сборки:

- bundle: `release/<platform>/onvif-ptz-controller/`
- архив: `release/onvif-ptz-controller-<platform>.zip`

Windows build уже проверен на запуск. Для Linux и macOS нужны native builds на соответствующих ОС.

## 8. Текущий результат

### 8.1 Реализовано

- подключение нескольких камер по ONVIF
- локальная библиотека сохранённых камер
- автообнаружение ONVIF-камер через WS-Discovery (мультикаст + unicast по локальным подсетям)
- browser-style вкладки камер
- Matrix mode
- поддержка камер без PTZ
- RTSP-видео через PyAV в изолированном дочернем процессе
- RTSP-аудио через sounddevice в том же дочернем процессе; регулировка громкости через `multiprocessing.Queue`
- PTZ: continuous / relative / absolute
- работа с пресетами
- Video Streaming editor
- Imaging Settings editor
- Profiles panel
- Network Settings
- Maintenance
- User Management
- standalone build для Windows и build-пайплайн для Linux/macOS

### 8.2 Не реализовано или ограничено

- отдельный слой внешнего API для интеграции с другими системами
- формальный артефакт пользовательского отзыва
- Linux/macOS standalone не проверялись в текущей рабочей среде
- часть ONVIF Device Management функций зависит от поддержки конкретной камеры
- WS-Discovery через IPv6 не реализован

## 9. Направления развития

Следующие технически логичные шаги:

- добавить поддержку WS-Discovery через IPv6
- вынести ONVIF-вызовы из GUI-слоя в неблокирующую модель task-based execution
- ввести автоматические smoke/integration tests для нескольких классов ONVIF-камер
- добавить инсталляторы для Windows, AppImage для Linux и подписанный `.app/.dmg` для macOS
