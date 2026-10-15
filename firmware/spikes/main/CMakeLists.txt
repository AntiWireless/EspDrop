# All sources compile in both modes; app_main.c selects console vs bridge at the
# C level (#if CONFIG_ESPDROP_BRIDGE_MODE), and the linker drops whatever the
# chosen mode does not call. REQUIRES is intentionally config-INDEPENDENT:
# IDF expands component requirements in an early pass where CONFIG_* is not yet
# set, so a config-conditional REQUIRES silently drops entries.
idf_component_register(
    SRCS "app_main.c" "radio.c" "console.c" "awdl_app.c" "bridge.c"
    INCLUDE_DIRS "."
    REQUIRES awdl esp_wifi esp_event esp_netif nvs_flash console esp_timer esp_driver_usb_serial_jtag
)
