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
| Автообнаружение камер по сети | Обязательно | Не реализовано |
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
- browser-style вкладки камер
- Matrix mode для одновременного просмотра нескольких камер
- редакторы Video Streaming, Imaging Settings и Profiles
- разделы Device Management: Network Settings, Maintenance, User Management
- standalone build для Windows и подготовленный пайплайн сборки для Linux/macOS

При этом два исходных пункта всё ещё остаются вне закрытого MVP по исходной формальной спецификации:

- автообнаружение камер по WS-Discovery
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
| `video_player.py` | Захват и декодирование RTSP-видео через PyAV |
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
- отдельный `QThread` для RTSP-видео
- отдельный `asyncio` loop на каждую подключённую камеру для ONVIF-запросов
- QtMultimedia для аудио RTSP-потока активной камеры

Это позволяет отделить декодирование видео от UI и одновременно удерживать модель «одна камера = одна логическая сессия».

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

Основной runtime API:

- `VideoStreamThread.set_url()`
- `VideoStreamThread.start()` / `stop_stream()`
- `build_rtsp_url()`

Логика потока сейчас:

- сначала пробует UDP
- если открыть поток через UDP не удаётся, переходит к TCP
- не принимает успехом «открытие контейнера без кадров»
- режет частоту отображения кадров, чтобы уменьшить визуальную задержку

## 5. ONVIF-интеграция

### 5.1 Используемые ONVIF-сервисы

При подключении приложение пытается создать следующие сервисы:

| Сервис | Статус |
|---|---|
| Device Management | Обязательный |
| Media | Обязательный |
| PTZ | Опциональный |
| Imaging | Опциональный |

Если PTZ или Imaging не поддерживаются камерой, приложение не падает: соответствующие разделы скрываются или переходят в read-only/empty state.

### 5.2 Основные ONVIF-операции

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

### 5.3 Особенности интеграции

- WSDL-файлы берутся из установленного пакета `onvif`
- часть операций зависит от конкретного вендора и прошивки камеры
- Device Management UI работает в best-effort режиме: приложение пытается применить доступные поля по частям, а не считает всю секцию недоступной при одном несовместимом параметре
- для `ZeroConfiguration` используется фактическая сигнатура ONVIF `GetZeroConfiguration()` без аргументов
- для hostname DHCP/manual используется отдельная операция `SetHostnameFromDHCP`

## 6. Сборка и выпуск

### 6.1 Сборка из исходников

```bash
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate     # Windows
pip install -r requirements.txt
python main.py
```

### 6.2 Standalone build

```bash
pip install -r requirements-build.txt
python build_standalone.py
```

Результат сборки:

- bundle: `release/<platform>/onvif-ptz-controller/`
- архив: `release/onvif-ptz-controller-<platform>.zip`

Windows build уже проверен на запуск. Для Linux и macOS нужны native builds на соответствующих ОС.

## 7. Текущий результат

### 7.1 Реализовано

- подключение нескольких камер по ONVIF
- локальная библиотека сохранённых камер
- browser-style вкладки камер
- Matrix mode
- поддержка камер без PTZ
- RTSP-видео через PyAV
- RTSP-аудио активной камеры через QtMultimedia
- PTZ: continuous / relative / absolute
- работа с пресетами
- Video Streaming editor
- Imaging Settings editor
- Profiles panel
- Network Settings
- Maintenance
- User Management
- standalone build для Windows и build-пайплайн для Linux/macOS

### 7.2 Не реализовано или ограничено

- автообнаружение камер по WS-Discovery
- отдельный слой внешнего API для интеграции с другими системами
- формальный артефакт пользовательского отзыва
- Linux/macOS standalone не проверялись в текущей рабочей среде
- часть ONVIF Device Management функций зависит от поддержки конкретной камеры

## 8. Направления развития

Следующие технически логичные шаги:

- добавить WS-Discovery
- вынести ONVIF-вызовы из GUI-слоя в неблокирующую модель task-based execution
- ввести автоматические smoke/integration tests для нескольких классов ONVIF-камер
- добавить инсталляторы для Windows, AppImage для Linux и подписанный `.app/.dmg` для macOS
