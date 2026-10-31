# AMD "POWERPLAY Application" trusted-application images

Three PSP trusted-application images extracted unmodified from AMD's Windows display driver
(amdkmdag.sys, Adrenalin package 32.0.31044.16, Microsoft Update Catalog). Each is an AMD-signed PSP
firmware image (header "$PS1" at +0x10, 0x100-byte header, Thumb code, load base 0x400000, 0x100-byte signature appended). The PSP
verifies the signature before running it; meat3 cannot alter them.

| file | size | notes |
|---|---|---|
| ppta_a.bin | 0xa100 | the newest/largest build; the Windows driver picks it unless PSP flag bit 20 is set |
| ppta_b.bin | 0x7100 | selected when bit 20 is set |
| ppta_c.bin | 0x64bc | selected when bits 20 and 14 are set |

Used by `meat3-fasttiming` (docs/rdna4.md) through the kernel's `ta_if` debugfs interface. The
images are AMD's property, redistributed here only so a Radeon owner can run AMD's own memory-timing
code on their own card under Linux.
