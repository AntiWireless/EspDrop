# All sources compile in both modes; app_main.c selects console vs bridge at the
# C level (#if CONFIG_ESPDROP_BRIDGE_MODE), and the linker drops whatever the
# chosen mode does not call. REQUIRES is config-INDEPENDENT (IDF expands
# requirements in an early pass where CONFIG_* is not yet set).
#
# The USB-Serial/JTAG driver lives in different components across IDF versions:
#   - 5.1 / 5.2 : the legacy "driver" component
#   - 5.3+      : "esp_driver_usb_serial_jtag"
# Default to the modern component: only pick "driver" when the version is
# positively detected as 5.1/5.2. (A missing/empty IDF_VERSION must not fall
# through to "driver", which on 5.3+ lacks the implementation and leaves
# usb_serial_jtag_driver_install undefined at link time.)
if(IDF_VERSION_MAJOR EQUAL 5 AND IDF_VERSION_MINOR LESS 3)
    set(ESPDROP_USJ_COMPONENT driver)
else()
    set(ESPDROP_USJ_COMPONENT esp_driver_usb_serial_jtag)
endif()
message(STATUS "EspDrop: target '${IDF_TARGET}', USB-Serial/JTAG via '${ESPDROP_USJ_COMPONENT}' (IDF ${IDF_VERSION_MAJOR}.${IDF_VERSION_MINOR})")

idf_component_register(
    SRCS "app_main.c" "radio.c" "console.c" "awdl_app.c" "bridge.c"
    INCLUDE_DIRS "."
    REQUIRES awdl esp_wifi esp_event esp_netif nvs_flash console esp_timer ${ESPDROP_USJ_COMPONENT}
)
