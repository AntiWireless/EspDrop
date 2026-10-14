idf_component_register(
    SRCS "app_main.c" "radio.c" "console.c"
    INCLUDE_DIRS "."
    REQUIRES awdl esp_wifi nvs_flash console esp_timer
)
