# StreamVault

StreamVault is a high-performance, self-hosted media streaming dashboard and desktop application. It integrates Telegram as a media vault, enabling users to stream video and audio content directly from private or public Telegram channels to their web browser or VLC player, bypassing standard limits using a local Telegram Bot API server or direct Telethon MTProto connection.

---

## 📸 Screenshots

Here are some previews of StreamVault in action:

1. **Dashboard & Media Library Overview**
   ![Dashboard Preview](https://via.placeholder.com/800x450.png?text=1.+Dashboard+%26+Media+Library+Overview)

2. **Album & Media Details**
   ![Album Details Preview](https://via.placeholder.com/800x450.png?text=2.+Album+%26+Media+Details)

3. **In-Browser HLS Video Player**
   ![HLS Player Preview](https://via.placeholder.com/800x450.png?text=3.+In-Browser+HLS+Video+Player)

4. **VLC Stream Player & Control Panel**
   ![VLC Player Integration](https://via.placeholder.com/800x450.png?text=4.+VLC+Stream+Player+%26+Control+Panel)

5. **Local Bot API Server & System Settings**
   ![Settings & Server Status](https://via.placeholder.com/800x450.png?text=5.+Local+Bot+API+Server+%26+System+Settings)

---

## 🛠️ Module Architecture

The application is modularized to separate responsibilities, keeping components clean and readable:

| Module | Purpose |
| :--- | :--- |
| **`alpha.py`** | Application bootstrap, initialization of services, and aiohttp web server entrypoint. |
| **`config.py`** | Environment variable management, credentials loading, client bootstrap, and global constants. |
| **`helpers.py`** | Media detection utilities, formatting functions, and metadata helpers. |
| **`cache.py`** | SQLite & file-based metadata cache, OMDb/TMDb fetching, encryption, and search index. |
| **`streaming.py`**| Low-level Telegram chunk fetching, HLS transcoding streams, and VLC-compatible streaming endpoints. |
| **`render.py`** | Dynamic HTML page builders, low-quality image placeholders (LQIP), and layout rendering. |
| **`routes.py`** | Web routes handling user authentication, stream serving, settings, and player pages. |
| **`electron.py`** | Native Electron desktop wrapper providing a frameless windows container for the application. |

---

## 🔒 Safety & Deployment Guidelines

To keep your credentials secure, **never push sensitive files to GitHub**. Below is a summary of what to push and what must remain ignored.

### 🚫 DO NOT Push (Ignored)
These files are automatically ignored in the `.gitignore` to prevent leaking private credentials or bloating the repository:
*   **`.env`**: Contains sensitive API keys, Telethon `API_HASH`, and Telegram `BOT_TOKEN`.
*   **`*.session` & `*.session-journal`**: Contains Telethon active session keys. If leaked, anyone can access your Telegram account.
*   **`*.db` / `*.db-shm` / `*.db-wal`**: Local cache databases, movie lists, and streaming history.
*   **`tg_cache.json` & `tg_albums.json`**: Cached indexes of your personal Telegram channels and chat media list.
*   **`node_modules/` & `venv/`**: Installed project dependencies.
*   **`build/` & `dist/`**: Local build packages.
*   **`test_tmp.bin`**: Giant testing binaries.

###  DO Push (Tracked)
*   All source code (`*.py`, `*.pyw`, `*.js`, `*.html`, `*.css`).
*   Config files (`package.json`, `package-lock.json`).
*   `.gitignore` & `README.md`.

---

## 🚀 Setup & Installation

### Prerequisites
1.  **Python 3.10+**
2.  **Node.js & npm** (for the Electron desktop client)
3.  **VLC Media Player** (optional, for external player streaming)

### 1. Configure the Environment
Create a `.env` file in the root directory:
```ini
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
BOT_TOKEN=your_bot_token
CHANNEL_ID=your_telegram_channel_id
PORT=5000
HOST=0.0.0.0
```

### 2. Run the Application

#### Option A: Running the Electron Desktop App (Recommended)
This boots the headless Python backend automatically and starts the Electron client:
```bash
python electron.py
```

#### Option B: Web UI Only
To run the server in web-only mode without the Electron GUI:
```bash
python alpha.py
```
Then visit `http://localhost:5000` in your browser.
