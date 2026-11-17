# Virtual display on Windows

A virtual display gives a simulator its own desktop while your physical monitor
remains available for other apps.

1. Install [Virtual Display Driver (VDD)](https://github.com/VirtualDrivers/Virtual-Display-Driver/releases)
   using its [official instructions](https://github.com/VirtualDrivers/Virtual-Display-Driver/wiki).
2. In VDD, add **1920 × 1080 at 100 Hz** if that mode is missing.
3. In Windows Display settings, extend the desktop to the virtual monitor and
   select that resolution and refresh rate.
4. In Goggles Bridge, choose the virtual monitor under **Display**, select
   **100 fps**, and start streaming. Move the simulator window there with
   **Win+Shift+Left/Right**.

The stream uses the normal capture pipeline and sends a 1920 × 1080 picture.
Manage the driver and its modes with VDD's tools. The virtual monitor stays
connected when you stop streaming or close Goggles Bridge; disable it in VDD or
Windows Display settings when you no longer need it.
