#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <time.h>
#include "esp_netif_sntp.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_sleep.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "driver/rmt_tx.h"
#include "mbedtls/sha256.h"
#include "cJSON.h"
#include "billboard_espidf.h"
#include "private_config.h"
#include "dashboard_ca.h"

#define FRAME_SIZE (2 * BILLBOARD_FRAME_BYTES)
#define MANIFEST_LIMIT 16384
#define MAGIC 0x43455245u
static const char *TAG = "billboard";
static RTC_DATA_ATTR struct {
    uint32_t magic;
    uint32_t boots;
    unsigned interval;
    char page[64];
    char hash[65];
} saved;
static EventGroupHandle_t wifi_events;
static bool wifi_initialized, wifi_started;
static unsigned reconnects;
static int supply_mv = -1;
static esp_netif_t *netif;
static esp_event_handler_instance_t wifi_handler, ip_handler;
static const gpio_num_t held_rtc[] = {1, 3, 10, 11, 13, 21};
static const int held_levels[] = {1, 1, 0, 0, 1, 0};

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) esp_wifi_connect();
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *event = data;
        ESP_LOGW(TAG, "Wi-Fi disconnect reason %d", event->reason);
        if (reconnects++ < 2) esp_wifi_connect();
        else xEventGroupSetBits(wifi_events, 2);
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = data;
        ESP_LOGI(TAG, "IP " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(wifi_events, 1);
    }
}

static esp_err_t connect_wifi(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    if (err != ESP_OK) return err;
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    netif = esp_netif_create_default_wifi_sta();
    if (!netif) return ESP_ERR_NO_MEM;
    esp_netif_set_hostname(netif, "billboard-dashboard");
    wifi_events = xEventGroupCreate();
    if (!wifi_events) return ESP_ERR_NO_MEM;
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    if ((err = esp_wifi_init(&init)) != ESP_OK) return err;
    wifi_initialized = true;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL, &wifi_handler));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL, &ip_handler));
    wifi_config_t config = {0};
    _Static_assert(sizeof(WIFI_SSID) <= sizeof(config.sta.ssid), "SSID too long");
    _Static_assert(sizeof(WIFI_PASSWORD) <= sizeof(config.sta.password), "Password too long");
    memcpy(config.sta.ssid, WIFI_SSID, sizeof(WIFI_SSID));
    memcpy(config.sta.password, WIFI_PASSWORD, sizeof(WIFI_PASSWORD));
    config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    config.sta.pmf_cfg.capable = true;
    config.sta.pmf_cfg.required = false;
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    if ((err = esp_wifi_start()) != ESP_OK) return err;
    wifi_started = true;
    esp_wifi_set_ps(WIFI_PS_NONE);
    EventBits_t bits = xEventGroupWaitBits(wifi_events, 3, pdFALSE, pdFALSE, pdMS_TO_TICKS(25000));
    if (!(bits & 1)) return ESP_ERR_TIMEOUT;
    // RTC time survives deep sleep. Cold boots must obtain time before TLS.
    if (time(NULL) < 1767225600) {
        esp_sntp_config_t time_config = ESP_NETIF_SNTP_DEFAULT_CONFIG(NTP_SERVER);
        err = esp_netif_sntp_init(&time_config);
        if (err != ESP_OK) return err;
        err = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(10000));
        esp_netif_sntp_deinit();
        if (err != ESP_OK || time(NULL) < 1767225600) return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

static void stop_wifi(void)
{
    if (wifi_initialized) {
        esp_event_handler_instance_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_handler);
        esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, ip_handler);
        if (wifi_started) esp_wifi_stop();
        esp_wifi_deinit();
        wifi_initialized = wifi_started = false;
    }
}

static esp_err_t get_request(const char *path, uint8_t *buffer, size_t capacity, size_t *used, bool telemetry)
{
    char url[256];
    int length = snprintf(url, sizeof(url), "%s%s", DASHBOARD_BASE, path);
    if (length < 0 || length >= sizeof(url)) return ESP_ERR_INVALID_SIZE;
    int64_t deadline = esp_timer_get_time()+20000000;
    esp_http_client_config_t config = {.url=url, .timeout_ms=5000,
        .disable_auto_redirect=true, .buffer_size=4096, .user_agent="BillboardDashboard/0.1",
        .cert_pem=dashboard_ca, .transport_type=HTTP_TRANSPORT_OVER_SSL};
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) return ESP_ERR_NO_MEM;
    esp_http_client_set_header(client, "Connection", "close");
    esp_http_client_set_header(client, "Authorization", "Bearer " DASHBOARD_TOKEN);
    if (telemetry && !strcmp(path, "/v1/manifest.json")) {
        char voltage[16];
        if (supply_mv >= 2500 && supply_mv <= 6000) snprintf(voltage, sizeof(voltage), "%d", supply_mv);
        else strcpy(voltage, "unknown");
        esp_http_client_set_header(client, "X-Badge-Supply-Mv", voltage);
    }
    *used = 0;
    esp_err_t result = esp_http_client_open(client, 0);
    if (result != ESP_OK) goto done;
    int64_t expected = esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    if (status != 200) {
        ESP_LOGW(TAG, "HTTP status %d", status);
        result = status == 429 ? ESP_ERR_NOT_FINISHED : ESP_FAIL;
        goto done;
    }
    if (expected < 1 || expected > capacity) {result=ESP_ERR_INVALID_SIZE; goto done;}
    while (*used < (size_t)expected) {
        if (esp_timer_get_time() > deadline) {result=ESP_ERR_TIMEOUT; goto done;}
        size_t remaining = (size_t)expected-*used;
        int count = esp_http_client_read(client, (char *)buffer+*used, remaining>4096?4096:remaining);
        if (count <= 0) {result=ESP_ERR_INVALID_RESPONSE; goto done;}
        *used += count;
    }
done:
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return result;
}

static esp_err_t get(const char *path, uint8_t *buffer, size_t capacity, size_t *used)
{
    esp_err_t result = get_request(path, buffer, capacity, used, true);
    if (result == ESP_ERR_NOT_FINISHED && !strcmp(path, "/v1/manifest.json"))
        result = get_request(path, buffer, capacity, used, false);
    return result;
}

static bool number_is(cJSON *object, const char *key, int value)
{
    cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
    return cJSON_IsNumber(item) && item->valuedouble == value;
}
static const char *string(cJSON *object, const char *key)
{
    cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
    return cJSON_IsString(item) ? item->valuestring : NULL;
}
static esp_err_t fetch_frame(uint64_t buttons, uint8_t **frame, char *new_hash, char *new_page)
{
    uint8_t *manifest = malloc(MANIFEST_LIMIT+1);
    if (!manifest) return ESP_ERR_NO_MEM;
    size_t count = 0;
    esp_err_t result = get("/v1/manifest.json", manifest, MANIFEST_LIMIT, &count);
    cJSON *root = NULL;
    if (result != ESP_OK) goto out;
    manifest[count] = 0;
    root = cJSON_Parse((char *)manifest);
    result = ESP_ERR_INVALID_RESPONSE;
    if (!root || !number_is(root, "version", 1) || !number_is(root, "width", BILLBOARD_WIDTH) ||
        !number_is(root, "height", BILLBOARD_HEIGHT)) goto out;
    cJSON *pages = cJSON_GetObjectItemCaseSensitive(root, "pages");
    if (!cJSON_IsArray(pages)) goto out;
    int total = cJSON_GetArraySize(pages), selected = 0;
    if (total < 1 || total > 32) goto out;
    for (int i=0; i<total; i++) {
        const char *id = string(cJSON_GetArrayItem(pages, i), "id");
        if (id && !strcmp(id, saved.page)) selected = i;
    }
    if (buttons & (1ULL<<15)) selected = (selected+1)%total;
    else if (buttons & (1ULL<<16)) selected = (selected+total-1)%total;
    cJSON *page = cJSON_GetArrayItem(pages, selected);
    const char *id = string(page, "id"), *hash = string(page, "sha256"),
               *path = string(page, "frame_url"), *format = string(page, "format");
    if (!id || strlen(id)>=64 || !hash || strlen(hash)!=64 || !path || !format ||
        strcmp(format, "black-red-msb-1") || !number_is(page, "bytes", FRAME_SIZE)) goto out;
    for (int i=0; i<64; i++) if (!isxdigit((unsigned char)hash[i])) goto out;
    char expected_path[96];
    snprintf(expected_path, sizeof(expected_path), "/v1/frames/%s.bin", hash);
    if (strcmp(path, expected_path)) goto out;
    cJSON *interval = cJSON_GetObjectItemCaseSensitive(root, "refresh_seconds");
    if (cJSON_IsNumber(interval) && interval->valuedouble >= 60 && interval->valuedouble <= 86400)
        saved.interval = (unsigned)interval->valuedouble;
    strcpy(new_hash, hash);
    strcpy(new_page, id);
    if (!strcmp(hash, saved.hash)) {
        ESP_LOGI(TAG, "Page %s unchanged; no panel refresh", id);
        result = ESP_OK;
        goto out;
    }
    *frame = heap_caps_malloc(FRAME_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!*frame) {result=ESP_ERR_NO_MEM; goto out;}
    result = get(path, *frame, FRAME_SIZE, &count);
    if (result != ESP_OK) goto out;
    if (count != FRAME_SIZE) {result=ESP_ERR_INVALID_SIZE; goto out;}
    uint8_t digest[32];
    if (mbedtls_sha256(*frame, FRAME_SIZE, digest, 0) != 0) {result=ESP_FAIL; goto out;}
    char actual[65];
    for (int i=0; i<32; i++) snprintf(actual+2*i, 3, "%02x", digest[i]);
    if (strcmp(actual, hash)) {result=ESP_ERR_INVALID_CRC; goto out;}
    for (size_t i=0; i<BILLBOARD_FRAME_BYTES; i++)
        if ((*frame)[i] & (*frame)[i+BILLBOARD_FRAME_BYTES]) {result=ESP_ERR_INVALID_RESPONSE; goto out;}
    ESP_LOGI(TAG, "Verified %u bytes for page %s", (unsigned)count, id);
out:
    cJSON_Delete(root);
    free(manifest);
    return result;
}

// GPIO14 is ADC2 on ESP32-S3; sample and release it BEFORE Wi-Fi starts.
static int sample_supply_mv(void)
{
    adc_unit_t unit;
    adc_channel_t channel;
    adc_oneshot_unit_handle_t adc = NULL;
    adc_cali_handle_t calibration = NULL;
    int millivolts = -1;
    if (adc_oneshot_io_to_channel(14, &unit, &channel) != ESP_OK) return -1;
    adc_oneshot_unit_init_cfg_t init = {.unit_id=unit, .ulp_mode=ADC_ULP_MODE_DISABLE};
    if (adc_oneshot_new_unit(&init, &adc) != ESP_OK) return -1;
    adc_oneshot_chan_cfg_t config = {.atten=ADC_ATTEN_DB_12, .bitwidth=ADC_BITWIDTH_DEFAULT};
    if (adc_oneshot_config_channel(adc, channel, &config) != ESP_OK) goto done;
    adc_cali_curve_fitting_config_t cal = {.unit_id=unit, .chan=channel,
        .atten=ADC_ATTEN_DB_12, .bitwidth=ADC_BITWIDTH_DEFAULT};
    if (adc_cali_create_scheme_curve_fitting(&cal, &calibration) != ESP_OK) goto done;
    int values[32], raw;
    // Discard initial samples and allow the high-impedance divider to settle.
    for (int i=0; i<4; i++) {
        adc_oneshot_read(adc, channel, &raw);
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    for (int i=0; i<32; i++) {
        if (adc_oneshot_read(adc, channel, &raw) != ESP_OK ||
            adc_cali_raw_to_voltage(calibration, raw, &values[i]) != ESP_OK) goto done;
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    // Trim both tails before averaging to reduce ADC outliers.
    for (int i=1; i<32; i++) {
        int value=values[i], j=i-1;
        while (j>=0 && values[j]>value) {values[j+1]=values[j]; j--;}
        values[j+1]=value;
    }
    int sum=0;
    for (int i=4; i<28; i++) sum += values[i];
    millivolts = (sum*2+12)/24; // Board's 1:2 divider senses regulator input.
done:
    if (calibration) adc_cali_delete_scheme_curve_fitting(calibration);
    adc_oneshot_del_unit(adc);
    return millivolts;
}

static void leds_off(void)
{
    gpio_set_direction(GPIO_NUM_1, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_1, 1); // Status LED active low.
    rmt_channel_handle_t channel = NULL;
    rmt_encoder_handle_t encoder = NULL;
    rmt_tx_channel_config_t config = {.gpio_num=GPIO_NUM_38, .clk_src=RMT_CLK_SRC_DEFAULT,
        .resolution_hz=10000000, .mem_block_symbols=64, .trans_queue_depth=1};
    rmt_copy_encoder_config_t copy_config = {};
    if (rmt_new_tx_channel(&config, &channel) == ESP_OK &&
        rmt_new_copy_encoder(&copy_config, &encoder) == ESP_OK && rmt_enable(channel) == ESP_OK) {
        rmt_symbol_word_t symbols[8*24];
        for (unsigned i=0; i<8*24; i++)
            symbols[i] = (rmt_symbol_word_t){.level0=1, .duration0=3, .level1=0, .duration1=9};
        rmt_transmit_config_t tx = {.loop_count=0, .flags.eot_level=0};
        if (rmt_transmit(channel, encoder, symbols, sizeof(symbols), &tx) == ESP_OK)
            rmt_tx_wait_all_done(channel, 100);
        rmt_disable(channel);
    }
    if (encoder) rmt_del_encoder(encoder);
    if (channel) rmt_del_channel(channel);
    gpio_set_direction(GPIO_NUM_38, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_38, 0);
}

static void release_holds(void)
{
    gpio_deep_sleep_hold_dis();
    gpio_hold_dis(GPIO_NUM_38);
    gpio_hold_dis(GPIO_NUM_47);
    for (unsigned i=0; i<sizeof(held_rtc)/sizeof(held_rtc[0]); i++) {
        rtc_gpio_hold_dis(held_rtc[i]);
        rtc_gpio_deinit(held_rtc[i]);
    }
    for (int pin=15; pin<=16; pin++) {
        rtc_gpio_hold_dis(pin);
        rtc_gpio_deinit(pin);
        gpio_set_direction(pin, GPIO_MODE_INPUT);
        gpio_set_pull_mode(pin, GPIO_PULLUP_ONLY);
    }
}

static void sleep_now(unsigned seconds)
{
    stop_wifi();
    // A held button must not trigger an immediate wake/retry loop.
    for (int i=0; i<20 && (!gpio_get_level(15) || !gpio_get_level(16)); i++)
        vTaskDelay(pdMS_TO_TICKS(25));
    uint64_t wake_pins = 0;
    for (int pin=15; pin<=16; pin++) {
        if (gpio_get_level(pin)) wake_pins |= 1ULL<<pin;
        rtc_gpio_init(pin);
        rtc_gpio_set_direction(pin, RTC_GPIO_MODE_INPUT_ONLY);
        rtc_gpio_pullup_en(pin);
        rtc_gpio_pulldown_dis(pin);
    }
    if (wake_pins) ESP_ERROR_CHECK(esp_sleep_enable_ext1_wakeup(wake_pins, ESP_EXT1_WAKEUP_ANY_LOW));
    ESP_ERROR_CHECK(esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000));
    for (unsigned i=0; i<sizeof(held_rtc)/sizeof(held_rtc[0]); i++) {
        rtc_gpio_init(held_rtc[i]);
        rtc_gpio_set_direction(held_rtc[i], RTC_GPIO_MODE_OUTPUT_ONLY);
        rtc_gpio_set_level(held_rtc[i], held_levels[i]);
        rtc_gpio_hold_en(held_rtc[i]);
    }
    gpio_set_direction(GPIO_NUM_47, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_47, 0);
    gpio_hold_en(GPIO_NUM_47);
    gpio_hold_en(GPIO_NUM_38);
    gpio_deep_sleep_hold_en();
    ESP_LOGI(TAG, "Deep sleep %u seconds; wake buttons mask 0x%llx; awake %lld ms",
             seconds, (unsigned long long)wake_pins, (long long)(esp_timer_get_time()/1000));
    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(100));
    esp_deep_sleep_start();
}

void app_main(void)
{
    esp_log_level_set("wifi", ESP_LOG_WARN);
    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
    uint64_t buttons = cause == ESP_SLEEP_WAKEUP_EXT1 ? esp_sleep_get_ext1_wakeup_status() : 0;
    if (saved.magic != MAGIC || cause == ESP_SLEEP_WAKEUP_UNDEFINED) {
        memset(&saved, 0, sizeof(saved));
        saved.magic = MAGIC;
        saved.interval = 3600;
        strcpy(saved.page, "home");
    }
    saved.boots++;
    release_holds();
    leds_off();
    supply_mv = sample_supply_mv();
    ESP_LOGI(TAG, "Input supply before Wi-Fi: %d mV", supply_mv);
    ESP_LOGI(TAG, "Billboard Dashboard, boot %lu, wake cause %d, PSRAM free %u",
        (unsigned long)saved.boots, cause, (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    uint8_t *frame = NULL;
    char new_hash[65] = {0}, new_page[64] = {0};
    esp_err_t result = connect_wifi();
    if (result == ESP_OK) result = fetch_frame(buttons, &frame, new_hash, new_page);
    stop_wifi();
    if (result != ESP_OK) {
        ESP_LOGW(TAG, "Update failed: %s; retaining display", esp_err_to_name(result));
    } else if (frame) {
        billboard_panel_t *panel = NULL;
        billboard_espidf_config_t config = billboard_espidf_default_config();
        result = billboard_espidf_create(&config, &panel);
        if (result == ESP_OK) {
            // A failed/partial physical refresh must never be mistaken for an unchanged image.
            saved.hash[0] = 0;
            ESP_LOGI(TAG, "Starting full black/red refresh (Wi-Fi off)");
            int display_result = billboard_panel_full_refresh(panel, frame,
                frame+BILLBOARD_FRAME_BYTES, BILLBOARD_FRAME_BYTES);
            ESP_LOGI(TAG, "Panel result %d, phase %s", display_result, panel->phase);
            int shutdown_result = billboard_espidf_destroy(panel);
            if (display_result == BILLBOARD_OK && shutdown_result == BILLBOARD_OK) {
                strcpy(saved.hash, new_hash);
                strcpy(saved.page, new_page);
                ESP_LOGI(TAG, "Panel refreshed and powered down");
            } else {
                ESP_LOGW(TAG, "Panel refresh/shutdown failed; retry at next scheduled wake");
            }
        } else {
            ESP_LOGW(TAG, "Panel initialization failed: %s", esp_err_to_name(result));
        }
    } else {
        strcpy(saved.page, new_page);
    }
    free(frame);
    sleep_now(TEST_SLEEP_SECONDS ? TEST_SLEEP_SECONDS : saved.interval);
}
