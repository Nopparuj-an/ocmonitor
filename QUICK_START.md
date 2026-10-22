# 🚀 Quick Start Guide

Get up and running with OpenCode Monitor in just a few minutes!

## 📋 Prerequisites

- Python 3.7 or higher
- pip package manager
- OpenCode session data: the SQLite database at `~/.local/share/opencode/opencode.db` (OpenCode v1.2.0+ and v2), or JSON files in `~/.local/share/opencode/storage/message/` for older versions

## 🛠️ Installation

### Option 1: pipx Installation (Recommended - Cross Platform)

[pipx](https://pypa.github.io/pipx/) is the recommended way to install Python CLI applications. It creates isolated environments and works on all platforms (including Arch Linux, Ubuntu, macOS, etc.).

```bash
git clone https://github.com/Shlomob/ocmonitor-share.git
cd ocmonitor-share
pipx install .
```

**Why pipx?**
- Creates isolated environments (no dependency conflicts)
- Works on Arch Linux without breaking system packages
- No sudo required
- Easy to upgrade or uninstall

**Optional extras:**
```bash
# With visualization charts
pipx install ".[charts]"

# With export functionality  
pipx install ".[export]"

# With all extras
pipx install ".[charts,export]"
```

### Option 2: Automated Installation (Linux/macOS)

```bash
git clone https://github.com/Shlomob/ocmonitor-share.git
cd ocmonitor-share
./install.sh
```

### Option 3: Manual Installation

```bash
git clone https://github.com/Shlomob/ocmonitor-share.git
cd ocmonitor-share
python3 -m pip install -r requirements.txt
python3 -m pip install -e .

# Add to PATH (if needed)
echo 'export PATH="$(python3 -m site --user-base)/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

## 🎯 First Steps

### 1. Check Configuration
```bash
ocmonitor config show
```

### 2. Analyze Your Sessions
```bash
# Analyze all sessions (uses default OpenCode directory)
ocmonitor sessions

# Analyze a specific session
ocmonitor session /path/to/specific/session
```

### 3. View Different Reports
```bash
# Daily usage breakdown
ocmonitor daily

# Model usage analytics
ocmonitor models

# Weekly breakdown
ocmonitor weekly
```

### 4. Export Data
```bash
# Export to CSV
ocmonitor export sessions --format csv --output my_report.csv

# Export to JSON
ocmonitor export sessions --format json --output my_report.json
```

### 5. Real-time Monitoring
```bash
# Start live dashboard
ocmonitor live
```

## 📖 Common Commands

| Command | Description |
|---------|-------------|
| `ocmonitor --help` | Show all available commands |
| `ocmonitor config show` | Display current configuration |
| `ocmonitor sessions` | Analyze all sessions |
| `ocmonitor session <path>` | Analyze a single session |
| `ocmonitor daily` | Daily usage breakdown |
| `ocmonitor models` | Model usage analytics |
| `ocmonitor live` | Real-time monitoring dashboard |
| `ocmonitor export <type> --format <csv/json>` | Export data |

## 🎨 Output Formats

All commands support different output formats:

```bash
# Rich tables (default)
ocmonitor sessions

# JSON output
ocmonitor sessions --format json

# Export to files
ocmonitor export sessions --format csv --output report.csv
```

## 🤔 Need Help?

- Run `ocmonitor <command> --help` for specific command help
- Check `MANUAL_TEST_GUIDE.md` for comprehensive usage examples
- File an issue on GitHub if you encounter problems

## 🎉 You're Ready!

Start exploring your OpenCode session data and gain insights into your AI-assisted coding patterns!