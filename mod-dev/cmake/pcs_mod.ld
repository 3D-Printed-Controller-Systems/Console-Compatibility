/* Link script for a 3DPCS mod. MIT License. pcsmod.py links every mod twice with different
   PCS_FLASH / PCS_RAM bases and turns the difference into a relocation list, so the app can place
   the mod anywhere in the mod area without a compiler. Don't hand-edit section order. */
ENTRY(pcs_mod_main)
MEMORY {
  FLASH (rx) : ORIGIN = PCS_FLASH, LENGTH = 256K
  RAM  (rwx) : ORIGIN = PCS_RAM,   LENGTH = 32K
}
SECTIONS {
  .text : ALIGN(4) {
    __pcs_image_start = .;
    KEEP(*(.pcs_entry))
    *(.text .text.*)
    *(.rodata .rodata.*)
    . = ALIGN(4);
  } > FLASH
  .data : ALIGN(4) {
    __pcs_data_start = .;
    *(.ramfunc .ramfunc.*)
    *(.data .data.*)
    . = ALIGN(4);
    __pcs_data_end = .;
  } > RAM AT > FLASH
  __pcs_data_load = LOADADDR(.data);
  .bss (NOLOAD) : ALIGN(4) {
    __pcs_bss_start = .;
    *(.bss .bss.* COMMON)
    . = ALIGN(4);
    __pcs_bss_end = .;
  } > RAM
  __pcs_image_end = __pcs_data_load + SIZEOF(.data);
  /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment) *(.note*) *(.init) *(.fini) }
}
