# Документация разработчика

## 1. Назначение документа

Этот документ описывает текущее состояние проекта ONVIF PTZ Controller для разработчиков:

- актуальную архитектуру приложения
- внутренний API модулей
- схему ONVIF-интеграции и WS-Discovery
- сборку и выпуск

## 2. Актуальная архитектура

### 2.1 Общая схема

```text
┌──────────────────────────────────────────────────────────────┐
│                    PyQt6 Desktop UI                          │
│                        main_window.py                        │
│                                                              │
│  Left rail        Center workspace         Right rail        │
│  Add Camera       Single tabs / Matrix     Info / PTZ        │
│  Saved Cameras    Active RTSP video        STR / IMG         │
│  WS-Discovery                              Profiles / NET    │
│                                            Maintenance / Users│
└──────────────────────────────┬───────────────────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         │                     │                     │
┌────────▼────────┐  ┌─────────▼────────┐  ┌────────▼────────┐
│ ONVIF SOAP      │  │ RTSP playback    │  │ WS-Discovery    │
│ onvif_client.py │  │ video_player.py  │  │ camera_         │
│ Device/Media    │  │ PyAV + FFmpeg    │  │ discovery.py    │
│ PTZ / Imaging   │  │ sounddevice audio│  │ UDP multicast / │
└────────┬────────┘  └─────────┬────────┘  │ unicast         │
         │                     │           └─────────────────┘
         └──────────┬──────────┘
                    │
          ┌─────────▼──────────┐
          │ ONVIF camera       │
          │ SOAP + RTSP        │
          └────────────────────┘
```

### 2.2 Основные модули

| Файл | Роль |
|---|---|
| `main.py` | Точка входа, логирование, создание `QApplication` и `MainWindow` |
| `main_window.py` | Основная логика UI, вкладки, Matrix mode, управление камерами и inspector-панели |
| `onvif_client.py` | Асинхронный ONVIF-клиент поверх `onvif-zeep-async` |
| `camera_discovery.py` | WS-Discovery: Probe-запросы, разбор ProbeMatch, определение локальных подсетей |
| `video_player.py` | RTSP-видео через PyAV в изолированном subprocess; аудио через sounddevice |
| `build_standalone.py` | Standalone-сборка через PyInstaller |
| `onvif_ptz_controller.spec` | PyInstaller spec |

### 2.3 Ключевые сущности

| Сущность | Где находится | Назначение |
|---|---|---|
| `CameraSession` | `main_window.py` | Состояние одной активной камеры: UI-страница, RTSP URI, asyncio loop, ONVIF client, media profiles, Matrix tile |
| `SavedCameraConfig` | `main_window.py` | Локально сохранённая конфигурация камеры |
| `DiscoveredCameraInfo` | `camera_discovery.py` | Устройство, найденное через WS-Discovery |
| `MediaProfileInfo` | `onvif_client.py` | Упрощённое представление ONVIF media profile |
| `ImagingSettingsPayload` | `onvif_client.py` | Imaging settings и options для динамического UI |
| `NetworkSettingsPayload` | `onvif_client.py` | Снимок Device Management network settings |
| `UserAccountInfo` | `onvif_client.py` | Строка пользователя камеры для User Management |

### 2.4 Потоки выполнения

Проект использует комбинированную модель:

- **GUI-поток** PyQt6 — интерфейс
- **`VideoStreamThread` (QThread)** — супервизор на каждую камеру; читает очередь от дочернего процесса и передаёт кадры в Qt
- **`multiprocessing.Process`** — изолированный FFmpeg-процесс; декодирует видео (PyAV) и воспроизводит аудио (sounddevice); изоляция защищает GUI от C-level крашей в кодеке
- **asyncio loop** — отдельный на каждую подключённую камеру для ONVIF-запросов
- **`_CameraDiscoveryWorker` (QThread)** — WS-Discovery в фоне, не блокирует GUI

Громкость передаётся в subprocess через `multiprocessing.Queue` (`cmd_q`). Быстрая остановка потока обеспечивается watcher-потоком внутри subprocess, который вызывает `container.close()` при срабатывании `stop_event`.

### 2.5 Локальные данные

Сохранённые камеры хранятся в `saved_cameras.json` в `QStandardPaths.AppConfigLocation`.

Если системный путь недоступен, используется резервная директория `.onvif-ptz-controller` в текущей рабочей папке.

## 3. Внутренний API

### 3.1 `main.py`

- `setup_logging()` — в frozen-сборке перенаправляет логи в `onvif_ptz_controller.log` рядом с exe
- `main()` — создаёт `QApplication` и `MainWindow`

### 3.2 `main_window.py`

`MainWindow` — orchestration-layer между UI, видеопотоком и ONVIF-клиентом.

| Группа | Примеры методов | Назначение |
|---|---|---|
| Управление камерами | `_on_connect`, `_connect_camera_config`, `_remove_camera_session` | Создание и удаление сессий |
| Видеопоток | `_start_video_for_session`, `_stop_video_for_session`, `_finish_start_video` | Жизненный цикл `VideoStreamThread` |
| Обновление UI | `_refresh_active_camera_ui`, `_show_empty_state` | Перерисовка инспектора |
| Matrix mode | `_move_session_to_matrix`, `_rebuild_matrix_grid`, `_on_matrix_tile_clicked` | Множественный просмотр |
| PTZ | `_on_ptz_move`, `_on_ptz_stop` | Проксирование команд в ONVIF client |
| Presets | `_on_refresh_presets`, `_on_goto_preset`, `_on_save_preset`, `_on_delete_preset` | Работа с пресетами |
| Media / Imaging | `_on_refresh_video_streaming`, `_on_apply_video_streaming`, `_on_refresh_imaging_settings`, `_on_apply_imaging_settings` | Encoder и imaging параметры |
| Device Management | `_on_refresh_network_settings`, `_on_apply_network_settings`, `_on_reboot_device`, `_on_refresh_user_accounts` | Сетевые и административные операции |
| Горячие клавиши | `_handle_global_key_event`, `_perform_ptz_action` | Shortcuts и PTZ hotkeys |

**Защита от гонки при старте потока.** `_start_video_for_session` инициирует асинхронный ONVIF-запрос `get_stream_uri`. Чтобы два параллельных callback не создали два `VideoStreamThread`, каждый старт помечается счётчиком `CameraSession._start_seq`; устаревший callback отбрасывается.

### 3.3 `onvif_client.py`

`ONVIFPTZClient` инкапсулирует все прямые обращения к ONVIF services.

| Категория | Методы |
|---|---|
| Подключение | `connect`, `disconnect`, `refresh_media_profiles`, `get_media_profiles` |
| PTZ | `continuous_move`, `relative_move`, `absolute_move`, `stop_move`, `get_status` |
| Presets | `get_presets`, `goto_preset`, `save_preset`, `delete_preset` |
| Media | `get_stream_uri`, `get_video_encoder_settings`, `set_video_encoder_settings`, `create_profile`, `edit_profile`, `delete_profile` |
| Imaging | `get_imaging_settings`, `set_imaging_settings` |
| Device Management | `get_network_settings`, `set_network_settings`, `get_user_accounts`, `create_user_account`, `update_user_account`, `delete_user_account`, `system_reboot`, `system_factory_reset`, `upgrade_firmware` |

### 3.4 `camera_discovery.py`

| Функция | Назначение |
|---|---|
| `discover_onvif_cameras(timeout, targets)` | Отправляет WS-Discovery Probe, собирает ProbeMatch, возвращает список `DiscoveredCameraInfo` |
| `parse_discovery_targets(text)` | Разбирает строку фильтра в список IP; поддерживает одиночные IP, частичные октеты, CIDR, списки |
| `default_discovery_targets(preferred_host)` | Возвращает адреса из локальных подсетей для автопоиска |
| `local_discovery_networks()` | Возвращает список `IPv4Network` физических интерфейсов |

Два режима поиска:
- **Multicast** — Probe на `239.255.255.250:3702`; камеры с WS-Discovery отвечают сами
- **Unicast** — Probe на каждый адрес из списка; используется при наличии локальных подсетей или поля Targets

На Windows подсети определяются через PowerShell `Get-NetIPAddress`; виртуальные интерфейсы (VMware, Hyper-V, Docker, WSL, Tailscale и др.) исключаются автоматически.

### 3.5 `video_player.py`

`VideoStreamThread` API:

| Метод / свойство | Назначение |
|---|---|
| `set_url(url)` | Устанавливает RTSP URL до запуска |
| `set_volume(v)` | Отправляет `('vol', v)` в `cmd_q` subprocess; применяется при следующем аудиокадре |
| `stop_stream()` | Устанавливает `stop_event`, ждёт завершения QThread (только если он запущен) |
| `is_running` | `True` пока subprocess-цикл активен |

Сигналы: `frame_ready(QImage)`, `stream_started()`, `stream_stopped()`, `error_occurred(str)`.

Логика `_rtsp_subprocess_worker`:
- Пробует UDP, при неудаче — TCP (чередуется по номеру попытки)
- Watcher-поток внутри subprocess вызывает `container.close()` при срабатывании `stop_event` → `demux()` разблокируется немедленно
- Громкость хранится в `current_vol`; обновляется через `cmd_q.get_nowait()` перед каждым аудиокадром
- При намеренной остановке (`stop_event.is_set()`) не запускает повторное подключение

## 4. ONVIF-интеграция

### 4.1 Используемые ONVIF-сервисы

| Сервис | Статус |
|---|---|
| Device Management | Обязательный |
| Media | Обязательный |
| PTZ | Опциональный |
| Imaging | Опциональный |

Если PTZ или Imaging не поддерживаются камерой, соответствующие разделы скрываются или переходят в empty state.

### 4.2 Основные ONVIF-операции

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
- `GetVideoEncoderConfiguration`, `GetVideoEncoderConfigurationOptions`
- `SetVideoEncoderConfiguration`
- `CreateProfile`, `DeleteProfile`

#### PTZ

- `ContinuousMove`, `RelativeMove`, `AbsoluteMove`, `Stop`, `GetStatus`
- `GetPresets`, `GotoPreset`, `SetPreset`, `RemovePreset`

#### Imaging

- `GetOptions`, `GetImagingSettings`, `SetImagingSettings`

### 4.3 Особенности интеграции

- WSDL-файлы берутся из установленного пакета `onvif`
- Device Management UI работает в best-effort режиме: поля применяются по частям, одна несовместимость не блокирует всю секцию
- Для `ZeroConfiguration` используется `GetZeroConfiguration()` без аргументов
- Для hostname DHCP/manual используется отдельная операция `SetHostnameFromDHCP`

## 5. Сборка и выпуск

### 5.1 Запуск из исходников

```bash
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate      # Windows
pip install -r requirements.txt
python main.py
```

### 5.2 Standalone build

```bash
pip install -r requirements-build.txt
python build_standalone.py
```

Результат:

- bundle: `release/<platform>/onvif-ptz-controller/`
- архив: `release/onvif-ptz-controller-<platform>.zip`

В windowed-сборке (`console=False`) логи пишутся в `onvif_ptz_controller.log` рядом с exe.

## 6. Реализовано

- подключение нескольких камер по ONVIF
- локальная библиотека сохранённых камер
- автообнаружение ONVIF-камер через WS-Discovery (multicast + unicast по локальным подсетям)
- browser-style вкладки камер
- Matrix mode
- поддержка камер без PTZ
- RTSP-видео через PyAV в изолированном дочернем процессе
- RTSP-аудио через sounddevice; регулировка громкости через `multiprocessing.Queue`
- PTZ: continuous / relative / absolute
- работа с пресетами
- Video Streaming editor
- Imaging Settings editor
- Profiles panel
- Network Settings
- Maintenance
- User Management
- standalone build для Windows, Linux и macOS
