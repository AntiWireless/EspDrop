idf_component_register(
    SRCS "app_main.c" "radio.c" "console.c" "awdl_app.c" "bridge.c"
    INCLUDE_DIRS "."
    REQUIRES awdl esp_wifi esp_event esp_netif nvs_flash console esp_timer esp_driver_usb_serial_jtag
)
