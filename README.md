# Job Monitor — Ubuntu edition

Monitors job/task pages from Ubuntu (VirtualBox), sends push notifications to your local Windows machine via **ntfy.sh** (free, no account needed).

## Quick start

### 1. Install dependencies (Ubuntu)
```bash
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
```

### 2. Subscribe on Windows
Open your Windows browser and go to:
```
https://ntfy.sh/<your-topic>
```
Replace `<your-topic>` with a unique string you set in `config.json` (e.g. `john-job-monitor-7x3q`).
Click **Subscribe**. Notifications will appear in that browser tab even when minimised.
Or install the [ntfy app](https://ntfy.sh/) on your phone/desktop.

### 3. Configure `config.json`
```json
{
  "ntfy": {
    "url": "https://ntfy.sh",
    "topic": "my-job-monitor-CHANGE-THIS"
  },
  "interval": 5,
  "headless": false,
  "timeWindow": { "alwaysOn": true },
  "sites": [
    {
      "name": "Handshake Available Tasks",
      "url": "https://app.joinhandshake.com/tasks",
      "targetTab": {
        "selector": "[role='tab']",
        "text": "Available Tasks"
      },
      "containerClassKeywords": "YOUR CONTAINER CLASSES HERE",
      "mode": "NEW"
    }
  ]
}
```

### 4. Find the container class names
```bash
python probe.py
```
The browser opens, navigates to each site, and prints whether the container was found.
If not found, open DevTools → inspect the element wrapping all job cards → copy some of its class names.

### 5. Run the monitor
```bash
python monitor.py
```
Browser opens (visible so you can log in). Leave it running.

---

## Config options

| Key | Default | Description |
|-----|---------|-------------|
| `ntfy.url` | `https://ntfy.sh` | ntfy server URL (use own server for privacy) |
| `ntfy.topic` | — | Unique topic name. **Change this.** |
| `interval` | `5` | Minutes between scans |
| `headless` | `false` | Set `true` to hide browser after first login |
| `timeWindow.alwaysOn` | `true` | If false, only scan between `from`→`to` |
| `sites[].name` | — | Label used in notifications and state file |
| `sites[].url` | — | Page URL to open |
| `sites[].targetTab.selector` | `[role='tab']` | CSS selector for tab elements |
| `sites[].targetTab.text` | — | Text of the tab to click (partial match, case-insensitive) |
| `sites[].containerClassKeywords` | — | Space-separated class names all on the container element |
| `sites[].mode` | `NEW` | `NEW` = only new jobs, `ANY` = notify whenever jobs exist |

## Handling same-URL tabs (Handshake "My Tasks" vs "Available Tasks")

Set `targetTab` per site. Use two separate site entries with the same URL but different `name` and `targetTab.text`:

```json
"sites": [
  {
    "name": "Handshake — Available Tasks",
    "url": "https://app.joinhandshake.com/tasks",
    "targetTab": { "selector": "[role='tab']", "text": "Available Tasks" },
    "containerClassKeywords": "...",
    "mode": "NEW"
  }
]
```

The script clicks the matching tab before scanning, so "My Tasks" count never contaminates "Available Tasks" state.

## State file
`state.json` stores seen job IDs per site (max 1000, oldest dropped). To reset a site's baseline, delete its entry from `state.json`.

## Running headless after login
1. First run: `"headless": false` — log in manually.
2. Credentials are saved in `browser_profile/`.
3. Set `"headless": true` — subsequent runs need no browser window.
