"""Notification-area (system tray) icon for the installed EveWatch.

Optional: needs the 'pystray' and 'Pillow' packages (bundled in the
installers). Without them EveWatch still works - use Help > Quit in the page.
"""


def run(name, icon_path, on_open, on_update, on_quit):
    """Show the tray icon and block until the user picks Quit.
    Returns True if the icon ran, False if the system can't show one."""
    try:
        import pystray
        from PIL import Image
        image = Image.open(icon_path)

        stop = {"icon": None}

        def quit_(icon, item):
            on_quit()
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem("Open " + name, lambda i, it: on_open(),
                             default=True),
            pystray.MenuItem("Check for updates", lambda i, it: on_update()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_),
        )
        icon = pystray.Icon(name, image, name, menu)
        stop["icon"] = icon
        icon.run()
        return True
    except Exception as e:  # noqa: BLE001 - no tray support / missing libs
        print("No tray icon available:", e)
        return False
