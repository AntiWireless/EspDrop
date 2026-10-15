# All sources compile in both modes; app_main.c selects console vs bridge at the
# C level (#if CONFIG_ESPDROP_BRIDGE_MODE), and the linker drops whatever the
# chosen mode does not call. REQUIRES is config-INDEPENDENT (IDF expands
# requirements in an early pass where CONFIG_* is not yet set), but it may depend
# on IDF_VERSION, which IS set that early.
#
# The USB-Serial/JTAG driver lives in different components across IDF versions:
#   - 5.1 / 5.2 : the legacy "driver" component
#   - 5.3+      : "esp_driver_usb_serial_jtag"
# Requiring the wrong one links but leaves usb_serial_jtag_driver_install
# undefined (header found via a common require, implementation not linked).
if(IDF_VERSION_MAJOR GREATER 5 OR (IDF_VERSION_MAJOR EQUAL 5 AND IDF_VERSION_MINOR GREATER_EQUAL 3))
    set(ESPDROP_USJ_COMPONENT esp_driver_usb_serial_jtag)
else()
    set(ESPDROP_USJ_COMPONENT driver)
endif()

idf_component_register(
    SRCS "app_main.c" "radio.c" "console.c" "awdl_app.c" "bridge.c"
    INCLUDE_DIRS "."
    REQUIRES awdl esp_wifi esp_event esp_netif nvs_flash console esp_timer ${ESPDROP_USJ_COMPONENT}
)
