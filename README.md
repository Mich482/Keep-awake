# 🖥️ KeepAwake

> A ridiculously over-engineered tray utility that prevents your Windows workstation from locking — complete with live nerd dashboard.

---

## 🚀 What is this?

**KeepAwake** is a lightweight Windows tray application that:

- Monitors system idle time
- Prevents the screen from locking by subtly "jiggling" the mouse
- Detects whether the workstation is unlocked
- Tracks detailed idle & wake statistics
- Hosts a live local nerd dashboard
- Starts automatically with Windows (optional)

It exists because sometimes adjusting sleep settings just isn’t enough 😉

---

## 🧠 Features

### 🔹 Smart Idle Detection
Uses the Windows `GetLastInputInfo` API to measure true user inactivity.

### 🔹 Intelligent Wake Logic
Mouse movement is only triggered when:

- Workstation is **unlocked**
- Idle threshold is reached

No pointless movement when already locked.

### 🔹 Live Dashboard
Hosted on `http://127.0.0.1:<dynamic_port>`

Displays:

- Uptime
- Current idle time
- Average / max idle
- Wake count
- Wakes per hour
- Last wake timestamp
- Time since last wake
- Risk level (low → critical)
- Armed/intercept state
- Live idle history chart (smooth gradient, threshold band, hover tooltip)
- Raw history toggle

Yes. It’s unnecessarily cool.

---

## 📊 Nerd States

The internal state machine reports:

| State      | Meaning |
|------------|----------|
| ACTIVE     | Recent user activity |
| IDLE       | Idle but safe |
| WARNING    | Approaching threshold |
| HIGH       | Close to interception |
| CRITICAL   | Threshold reached |
| INTERCEPT  | Mouse movement triggered |
| LOCKED     | Workstation locked |

Risk is calculated as:

```
idle_seconds / threshold_seconds
```

And categorized dynamically.

---

## 🛠 Build (PyInstaller)

### Recommended (stable & fast startup)

```
pyinstaller --noconsole --onedir --name KeepAwake --icon icons/app.ico app.py
```

### Optional onefile build

```
pyinstaller --noconsole --onefile --name KeepAwake --icon icons/app.ico app.py
```

If bundling assets manually via `.spec`:

```
datas=[('icons', 'icons')]
```

---

## 📂 Project Structure

```
KeepAwake/
├── app.py
├── icons/
│   └── app.ico
└── README.md
```

---

## ⚙ Configuration (in code)

```python
IDLE_THRESHOLD_SECONDS = 240
CHECK_INTERVAL = 2
HISTORY_LEN = 240
```

Adjust as desired.

---

## 🔒 Single Instance Protection

Uses a named Windows mutex:

```
Local\KeepAwakeMutex
```

Prevents multiple tray instances.

---

## 💻 System Requirements

- Windows 10 / 11
- Python 3.11+ (for development)
- PyInstaller (for packaging)

Runtime exe requires no admin rights.

---

## 🧪 Resource Usage

Typical idle footprint:

- Memory: ~20–40 MB (Python runtime dominated)
- CPU: ~0% (periodic 2s polling)

Mouse movement: 1–2 pixels.

Extremely low overhead.

---

## 🧩 Why not just change sleep settings?

Because:

- Corporate group policies exist
- Remote desktop sessions behave differently
- You want observability
- You enjoy overengineering small problems

---

## 📈 Dashboard Preview

- Smooth idle curve
- Threshold band shading
- Hover tooltips
- Color-coded risk levels
- Persistent raw history toggle

Yes, this is for a tray app.

---

## 🧑‍💻 Author

Built for people who:

- Appreciate clean state machines
- Enjoy WinAPI under the hood
- Love small tools done properly

---

## 📜 License

MIT — do whatever you want, just don’t blame me if your mouse moves.

---

> "There is nothing more permanent than a temporary solution that works."

