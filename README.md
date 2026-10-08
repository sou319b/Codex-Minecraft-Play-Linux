# Codex Minecraft Play Linux

A local MCP server that lets Codex see and control Minecraft on Ubuntu/GNOME Wayland. It captures an approved window and sends mouse and keyboard input through XDG Desktop Portals and PipeWire.

Supports screenshots, movement, clicks, key combinations, scrolling, and short action sequences. Runs as your desktop user; no API key is required by the server.

Inspired by [Codex-Minecraft-Gameplay](https://github.com/wz1119/Codex-Minecraft-Gameplay) by [wz1119](https://github.com/wz1119).

## Setup

Requires Ubuntu/GNOME Wayland, Python 3.10+, and a logged-in desktop session. Other desktop environments are untested.

Install dependencies:

```bash
sudo apt update
sudo apt install git python3-dbus python3-gi python3-pil \
  gir1.2-gstreamer-1.0 gir1.2-gtk-3.0 \
  gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  pipewire xdg-desktop-portal xdg-desktop-portal-gnome
```

Clone the repository and check your environment:

```bash
git clone https://github.com/sou319b/Codex-Minecraft-Play-Linux.git
cd Codex-Minecraft-Play-Linux
/usr/bin/python3 server.py doctor
```

The capture plugins reported by `doctor` should all be `true`.

Register the server with the Codex CLI from this directory:

```bash
codex mcp add linux-computer-use -- /usr/bin/python3 "$PWD/server.py" serve
```

Restart the MCP connection in Codex. For manual configuration, use [mcp-config.example.toml](mcp-config.example.toml) and replace the example path with your repository's absolute path.

## Usage

Open Minecraft, then ask Codex:

> Use linux-computer-use to connect to my Minecraft window and take a screenshot. Once I focus Minecraft, move forward for one second and show the result. Disconnect when finished.

Accept the OS sharing dialog within 45 seconds and select the Minecraft window. **Keep Minecraft focused: input goes to the currently focused app, even when sharing only one window.**

The usual flow is `linux_desktop_connect` → `linux_desktop_screenshot` → `linux_desktop_act` → `linux_desktop_disconnect`. Keep actions short; each hold is limited to five seconds. Coordinates use the latest screenshot's pixels. After an error, inspect the screen before repeating an action.

To stop input from another terminal:

```bash
/usr/bin/python3 server.py stop
```

After checking the screen, run `/usr/bin/python3 server.py reset-stop` to allow input again. Stop requests may wait for an in-flight capture or portal call to finish. To end sharing, ask Codex to disconnect or use the OS sharing indicator.

## Development

Run the tests without sending desktop input:

```bash
/usr/bin/python3 -m unittest -v
```
