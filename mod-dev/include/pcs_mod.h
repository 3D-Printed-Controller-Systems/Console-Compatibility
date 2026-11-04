/* pcs_mod.h: the 3DPCS firmware mod API.  MIT License, see LICENSE.mod-api.
 *
 * API 0.5: firmware-supplied random bytes. Append-only API table; check
 * major/minor and size before using new functions. Public API license: LICENSE.mod-api.
 *
 * A mod is a small precompiled add-on. The app places it in the controller's mod area when it flashes, so nobody needs
 * a compiler to install one. A mod never links against the firmware: it gets everything through the pcs_api_t table
 * handed to pcs_mod_main(), and gives back a pcs_mod_t with the hooks it wants. See mod-dev/README.md.
 *
 * Rules every mod follows:
 *  - Hooks return quickly (task: under 200 us typical, 1 ms worst). No busy waits; use api->time_us() and come back
 *    next loop. A hook that stalls the controller trips the watchdog and counts as a crash.
 *  - Hardware is claimed before it is touched (api->claim_*). A failed claim means the controller uses it: report it
 *    with api->fail() and the app shows the reason.
 *  - No direct flash writes, no core 1, no interrupt priority changes. Saved settings go through api->settings_*.
 *  - Use the pico-sdk hardware headers only for register access (hardware/structs/..., static inline helpers).
 *    Anything that keeps state in the firmware (claims, IRQ handlers, I2C / SPI drivers shared with the controller)
 *    goes through this table, because the mod can't see the firmware's copy of pico-sdk.
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PCS_MOD_API_MAJOR 0
#define PCS_MOD_API_MINOR 5
#define PCS_MOD_MAGIC     0x4D534350u   /* "PCSM" */

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------- what the mod sees of the controller */

/* Live inputs, product neutral (every controller fills what it has; the rest stays 0). on_inputs may change them
 * before the controller sends its report, e.g. remap a button or add a turbo. */
typedef struct {
    uint32_t buttons;        /* PCS_BTN_* bits */
    int16_t  axis[8];        /* PCS_AX_*: -32768..32767, or 0..32767 for one-sided values (whammy) */
    uint8_t  hat;            /* 0..7 = N, NE, E ... NW, 8 = centered */
    uint8_t  flags;          /* PCS_IN_* */
    uint16_t reserved;
} pcs_inputs_t;

enum {  /* guitar names; other products map their buttons onto the same bits (see their MODULE docs) */
    PCS_BTN_GREEN = 1u << 0, PCS_BTN_RED = 1u << 1, PCS_BTN_YELLOW = 1u << 2, PCS_BTN_BLUE = 1u << 3,
    PCS_BTN_ORANGE = 1u << 4, PCS_BTN_STRUM_UP = 1u << 5, PCS_BTN_STRUM_DOWN = 1u << 6, PCS_BTN_START = 1u << 7,
    PCS_BTN_SELECT = 1u << 8, PCS_BTN_GUIDE = 1u << 9, PCS_BTN_STICK = 1u << 10,
    PCS_BTN_USER0 = 1u << 16                                   /* 16..31: free for the product or a mod */
};
enum { PCS_AX_WHAMMY = 0, PCS_AX_TILT = 1, PCS_AX_JOY_X = 2, PCS_AX_JOY_Y = 3, PCS_AX_SLIDER = 4,
       PCS_AX_USER0 = 5 };
enum { PCS_IN_TILT_ACTIVE = 1u << 0, PCS_IN_LEFTY = 1u << 1, PCS_IN_ON_BATTERY = 1u << 2 };

/* One frame of lights, after the controller's own effects. on_leds may change any of them. */
typedef struct {
    uint8_t  count;          /* lights on this controller (names in pcs_board_t) */
    uint8_t  brightness;     /* 0..255 applied after on_leds */
    uint16_t reserved;
    uint8_t  (*rgb)[3];      /* count entries */
} pcs_leds_t;

typedef struct {
    const char *module_id;   /* "polybar", "hammeron", ... */
    const char *profile_id;  /* the detected board, e.g. "usk_v2" */
    uint8_t     fw_version[3];
    uint8_t     led_count;
    const char *const *led_names;  /* "Green", "Strum up", ... same names as palettes */
    uint32_t    pins_used;   /* GPIOs the controller itself uses (bit n = GPn); the rest are free for mods */
    bool        has_radio;   /* Pico W / RM2 */
} pcs_board_t;

/* ---------------------------------------------------------------- USB
 * PCS_USB_ADD: the mod's interfaces sit next to the controller's own (a guitar that is also a MIDI device).
 * PCS_USB_REPLACE: the mod is the whole device (an RC car remote, a robot arm). The controller's game pad goes away.
 * PCS_USB_CONDITIONAL (API 0.3): the controller stays its stock self (gamepad + PCS Config) until the mod switches it
 *   to a USB personality it declared, then re-enumerates back when the host goes away. For one controller that is a PC
 *   gamepad in one place and, say, your own project's device in another, from a single mod. See "conditional USB" below.
 * In every mode the settings interface stays last, so the app can always reach the controller and turn the mod off. */
typedef enum { PCS_USB_NONE = 0, PCS_USB_ADD = 1, PCS_USB_REPLACE = 2, PCS_USB_CONDITIONAL = 3 } pcs_usb_mode_t;

/* One endpoint of a mod interface. The firmware picks the endpoint number and returns it from usb_add. */
typedef struct {
    uint8_t  in;                  /* 1 = IN (to the computer), 0 = OUT */
    uint8_t  type;                /* PCS_EP_BULK or PCS_EP_INTERRUPT */
    uint16_t size;                /* max packet: 8..64 */
    uint8_t  interval;            /* interrupt: ms between polls (1..255); bulk: 0 */
    uint8_t  extra_len;           /* class-specific descriptors right after this endpoint's descriptor (MIDI) */
    uint16_t reserved;
    const uint8_t *extra;
} pcs_usb_ep_t;
enum { PCS_EP_BULK = 2, PCS_EP_INTERRUPT = 3 };

/* One interface. Call usb_add once per interface, in init. The firmware reads the descriptor pointers when a computer
 * asks for them, so they must stay valid (static, or in the mod's RAM): a mod may fill in interface numbers it only
 * learns from usb_add (MIDI's audio-control header names the streaming interface).
 * HID: give hid_report_desc and leave class_desc NULL; the firmware writes the HID class descriptor, answers
 * GET_DESCRIPTOR(Report) and SET_IDLE / SET_PROTOCOL. Other class requests go to on_usb_setup. */
typedef struct {
    uint8_t  cls, subclass, protocol;
    uint8_t  ep_count;            /* 0..4 */
    pcs_usb_ep_t ep[4];
    const uint8_t *class_desc;    /* descriptors between the interface and its endpoints (MIDI headers and jacks ...) */
    uint16_t class_desc_len;
    uint8_t  group;               /* first interface of a function with `group` interfaces (MIDI: 2): the firmware
                                     adds an interface association descriptor. 0 = on its own */
    uint8_t  reserved;
    const char *name;             /* interface string, ASCII, 32 max */
    const uint8_t *hid_report_desc;   /* HID (cls 3) only */
    uint16_t hid_report_desc_len;
} pcs_usb_iface_t;

typedef struct { uint8_t bmRequestType, bRequest; uint16_t wValue, wIndex, wLength; } pcs_usb_setup_t;

/* ---------------------------------------------------------------- conditional USB (API 0.3)
 * A "personality" is one whole USB identity: the device-level fields below plus the interfaces added for it. The mod
 * declares each personality in init (usb_personality_add, then usb_add for its interfaces), and asks the core to
 * re-enumerate between them at runtime (usb_switch). Personality 0 is the controller's stock identity, built by the
 * firmware; the mod never declares it. This is plumbing only: the mod supplies its own descriptor bytes and decides
 * when to switch. Project-specific protocol behavior belongs in the mod. */
typedef struct {
    uint16_t usb_version;        /* bcdUSB (e.g. 0x0200); 0 = firmware default */
    uint16_t device_release;     /* bcdDevice; 0 = derived from the mod so a host re-reads it */
    uint16_t vid, pid;           /* 0 = the shared 3DPCS maker id with `product` as the name */
    uint8_t  dev_class, dev_subclass, dev_protocol;
    uint8_t  attributes;         /* configuration bmAttributes (self-power, remote wakeup); 0 = bus-powered default */
    uint16_t max_power_ma;       /* 0 = default */
    uint16_t flags;              /* PCS_USBDEV_* */
    const char *manufacturer;    /* NULL = default */
    const char *product;         /* NULL = the mod's usb_name */
} pcs_usb_device_t;
enum { PCS_USBDEV_MS_OS_DESC = 1u << 0 };   /* emit Microsoft OS descriptors so Windows binds WinUSB to a vendor
                                               interface without a driver (for your own PC software to talk to it) */

/* What the current host has asked for, read-only (usb_host()). Evidence, never proof: firmware-update tools, BIOS
 * menus, hubs and unusual operating systems can all look alike, so treat a missing request as a hint and keep a
 * timeout fallback. `classification` / `confidence` are the mod's own, set from what it matches; 0 = undecided. */
typedef struct {
    uint16_t flags;              /* PCS_HOST_* */
    uint8_t  classification;     /* mod-defined; 0 = unknown */
    uint8_t  confidence;         /* 0..255, the mod's own */
    uint32_t session;            /* bumps on every (re-)enumeration, so stale work can be dropped */
} pcs_usb_host_t;
enum { PCS_HOST_GOT_DEVICE_DESC = 1u << 0, PCS_HOST_GOT_CONFIG_DESC = 1u << 1, PCS_HOST_GOT_STRINGS = 1u << 2,
       PCS_HOST_GOT_MS_OS = 1u << 3, PCS_HOST_CONFIGURED = 1u << 4, PCS_HOST_SUSPENDED = 1u << 5 };

enum { PCS_USBP_ENTER = 0, PCS_USBP_EXIT = 1, PCS_USBP_RESET = 2 };   /* on_usb_personality phase */

/* Core-owned device exchange providers (API 0.4). Optional separately built native mods.
 * NONE USB mode only; one provider per boot. Hooks use the ordinary mod watchdog.
 * CONTROL: OUT data has already arrived; return 0 to accept, -1 to stall.
 * IN: fill at most capacity bytes in reply, return their count, or -1 to stall.
 * RESET: discard per-host state. Borrowed buffers expire when the hook returns.
 * Queue expensive work in task(); never busy-wait in this callback.
 * The core supplies a bounded control envelope; protocol meaning stays in the provider.
 * Claims are cooperation/ownership, not a sandbox. Native mods are trusted code. */
enum { PCS_SERVICE_DEVICE_EXCHANGE = 1 };
enum { PCS_SERVICE_CONTROL = 0, PCS_SERVICE_RESET = 1 };
typedef struct {
    uint8_t event, reserved;
    uint16_t length, capacity;
    pcs_usb_setup_t setup;
    uint8_t data[64];
} pcs_service_request_t;
typedef struct {
    uint16_t size;
    uint8_t service, reserved;
    int32_t (*handle)(const pcs_service_request_t *request, uint8_t *reply);
} pcs_usb_service_t;

/* ---------------------------------------------------------------- linked boards */
typedef struct { uint8_t node; uint8_t role; char module_id[24]; uint32_t serial; } pcs_node_t;
enum { PCS_LINK_CH_MOD0 = 8, PCS_LINK_CH_MAX = 15 };   /* channels 0..7 belong to the firmware */
/* Over the Polylink (firmware 0.9.95, Polylink 0.3.37): the same calls reach the board on the other end of the
   wireless link. On a Polylink, nodes 16-19 are the controllers in slots 1-4; on a controller, node 20 is the
   Polylink it's connected to. Listed only while messages can flow (both ends new enough). */
enum { PCS_NODE_HUB = 0, PCS_NODE_SLOT0 = 16, PCS_NODE_POLYLINK = 20 };
enum { PCS_ROLE_HUB = 0, PCS_ROLE_NODE = 1, PCS_ROLE_CONTROLLER = 2, PCS_ROLE_POLYLINK = 3 };   /* pcs_node_t.role */

/* ---------------------------------------------------------------- the API table (firmware -> mod) */
typedef struct pcs_api {
    uint16_t major, minor;            /* check minor before using a function added later */
    uint16_t size;                    /* sizeof(pcs_api_t) on the firmware side */
    uint16_t reserved;

    /* info and logging */
    const pcs_board_t *(*board)(void);
    void     (*log)(const char *fmt, ...);            /* to the app's log (Advanced). printf subset: %d %u %x %s %c */
    void     (*fail)(const char *why);                /* stop this mod, show `why` in the app ("GP5 is in use") */

    /* time */
    uint64_t (*time_us)(void);
    uint32_t (*time_ms)(void);

    /* claims: false = the controller or another mod already has it */
    bool     (*claim_pin)(uint8_t gpio);
    bool     (*claim_pio_sm)(uint8_t pio, uint8_t sm);
    int      (*claim_pio_program)(uint8_t pio, const uint16_t *instr, uint8_t len, int8_t origin); /* offset or -1 */
    int      (*claim_dma)(void);                      /* channel or -1 */
    int      (*claim_alarm)(void);                    /* hardware alarm 0..3 or -1 */
    /* firmware 0.9.91+ (older: false / -1). I2C a = SDA, b = SCL; SPI a = SCK, b = TX (claim SCK - 2 for MISO);
       UART a = TX, b = RX, hz = baud. The controller's own I2C on the same pins can be shared. */
    bool     (*claim_bus)(uint8_t kind, uint8_t unit, uint8_t pin_a, uint8_t pin_b, uint32_t hz); /* kind: 0 I2C, 1 SPI, 2 UART */
    bool     (*irq_add)(uint8_t irq, void (*handler)(void));   /* init only, for something the mod claimed; shared handler */

    /* shared peripherals (the controller may use the same bus or ADC) */
    int      (*i2c_xfer)(uint8_t unit, uint8_t addr, const uint8_t *tx, size_t tx_len, uint8_t *rx, size_t rx_len); /* <0 error */
    int      (*spi_xfer)(uint8_t unit, uint8_t cs_pin, const uint8_t *tx, uint8_t *rx, size_t len);   /* cs 0xFF = none */
    int      (*adc_read)(uint8_t input);              /* 0..4095, -1 if the pin isn't claimed; never blocks long */

    /* saved settings: this mod's own block (the size in its manifest, 256 bytes max). Laid out by its settings
       schema, which the app draws as a panel. */
    size_t   (*settings_read)(void *out, size_t max);
    bool     (*settings_write)(const void *data, size_t len);   /* saved soon after, not instantly */

    /* USB (mods with "usb" in their manifest) */
    bool     (*usb_add)(const pcs_usb_iface_t *iface, uint8_t *iface_num, uint8_t *ep_addrs); /* init only. ep_addrs:
                                                         one per endpoint, 0x81.. for IN, 0x01.. for OUT */
    bool     (*usb_ready)(void);                      /* a computer has set the device up */
    bool     (*usb_send)(uint8_t ep, const void *data, uint16_t len);  /* IN, len <= size. false = busy, try again later */
    bool     (*usb_control_reply)(const void *data, uint16_t len);     /* only inside on_usb_setup. Not called = the
                                                         request is refused (STALL), except OUT requests, which are accepted */

    /* linked boards */
    uint8_t  (*link_nodes)(pcs_node_t *out, uint8_t max);
    bool     (*link_send)(uint8_t node, uint8_t channel, const void *data, uint8_t len);   /* len <= 60 */

    /* math: the RP2040 boot ROM's fast float routines (no FPU on the chip), already looked up */
    const void *rom_float;

    /* memory helpers (mods are built without a C library) */
    void    *(*memcpy)(void *d, const void *s, size_t n);
    void    *(*memset)(void *d, int c, size_t n);

    /* conditional USB (API 0.3, "usb":"conditional"). Older firmware (minor < 3) leaves these NULL: check
       api->minor >= 3 before calling. See the pcs_usb_device_t / pcs_usb_host_t block above. */
    int      (*usb_personality_add)(const pcs_usb_device_t *dev);  /* init only: begin a personality; the usb_add calls
                                                         that follow fill it. Returns its id (>= 1), or -1 */
    const pcs_usb_host_t *(*usb_host)(void);          /* what the current host has asked for so far (read-only) */
    bool     (*usb_switch)(int id);                   /* re-enumerate into personality id (0 = stock). false if it is
                                                         already current, unknown, or a switch is already in flight */
    int      (*usb_current)(void);                    /* the personality that is live now (0 = stock) */
    /* API 0.4: init only. Registration enables the core-owned service's output capability.
       The provider does not choose output policy itself; compatible firmware may select its core-owned personality
       in Automatic mode. Keep provider in mod static memory. */
    bool     (*usb_service_claim)(const pcs_usb_service_t *provider);
    /* API 0.5: random bytes from the firmware's pico_rand entropy pool. Intended for nonces, salts and protocol
       challenges; max 64 bytes per call. This is not a substitute for isolating untrusted native mod code. */
    bool     (*random_fill)(void *out, size_t len);
} pcs_api_t;

/* ---------------------------------------------------------------- the mod's hooks (mod -> firmware). All optional. */
typedef struct pcs_mod {
    uint16_t api_major, api_minor;    /* PCS_MOD_API_* it was built against */
    uint16_t size;                    /* sizeof(pcs_mod_t) */
    uint8_t  usb_mode;                /* pcs_usb_mode_t */
    uint8_t  reserved;

    bool (*init)(void);                               /* claims, USB interfaces. false = don't run (call fail() first) */
    void (*task)(void);                               /* every loop */
    void (*on_inputs)(pcs_inputs_t *in);              /* before every report */
    void (*on_leds)(pcs_leds_t *leds);                /* before every light update */
    void (*on_settings)(void);                        /* the app changed this mod's settings */
    void (*on_usb_setup)(const pcs_usb_setup_t *s, const void *data);  /* class / vendor requests for its interfaces;
                                                         data = the OUT data stage (NULL for IN requests) */
    void (*on_usb_rx)(uint8_t ep, const void *data, uint16_t len);   /* OUT data on one of its endpoints */
    void (*on_link)(uint8_t node, uint8_t channel, const void *data, uint8_t len);
    void (*on_node)(const pcs_node_t *n, bool joined);
    void (*before_reboot)(void);                      /* lights off, motors stopped: return quickly */
    /* conditional USB (API 0.3). Optional, like the rest. */
    void (*on_usb_host)(const pcs_usb_host_t *host);  /* the host fingerprint changed: the mod may usb_switch() */
    void (*on_usb_personality)(int id, uint8_t phase);/* a transition: PCS_USBP_ENTER (id is live), EXIT (before it
                                                         leaves), RESET (bus reset) — reset protocol state here */
} pcs_mod_t;

/* The one symbol every mod defines. Called once at start-up, before USB starts. */
const pcs_mod_t *pcs_mod_main(const pcs_api_t *api);

#ifdef __cplusplus
}
#endif
