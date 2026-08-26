/* Report a CXL 1.1 RCD's negotiated link width and speed.
 *
 * There is no other way to get this. A CXL 1.1 device attaches as a Root
 * Complex Integrated Endpoint, and an RCiEP is not required to implement the
 * PCIe Link Capability/Status registers -- on the parts we have it does not, so
 * `lspci -vv` prints no LnkCap/LnkSta line and sysfs reports
 * current_link_speed=Unknown, current_link_width=0. The registers that do
 * exist belong to the *host bridge's downstream port*, and they live in the
 * RCRB, a memory-mapped block whose address only appears in the ACPI CEDT.
 * So: parse CEDT -> find the CHBS entry -> mmap the RCRB -> walk the PCI
 * capability chain -> read LnkCap/LnkSta.
 *
 * This mattered. Two hosts carrying the identical part (same PCI IDs, same
 * firmware) measured 11.8 vs 26.0 GB/s, and every software explanation we
 * tested came back negative: thread count, socket affinity, copy-vs-load,
 * page size (2 MiB PMD confirmed), dax mode, and AER (192 GiB moved with a
 * cleared error register and nothing logged). One read of these registers gave
 * the answer:
 *
 *   slow host   LnkCap max 16 GT/s x16   LnkSta negotiated 16 GT/s x8  15.8 GB/s
 *   fast host   LnkCap max 32 GT/s x16   LnkSta negotiated 32 GT/s x8  31.5 GB/s
 *
 * Same width. The gap is entirely link *speed* -- one port runs the device at
 * Gen4, the other at Gen5, and the per-lane rate doubles. Measured throughput
 * came to 75% and 83% of those ceilings, both ordinary for CXL.mem.
 *
 * Read the speed line first. A narrower negotiated width is often not a fault:
 * a x8 device in a x16 port negotiates x8 and that is correct. Compare the
 * negotiated width against the *device's* width from its datasheet, not
 * against the port maximum -- the port maximum tells you about the slot.
 *
 *   gcc -O2 -o cxl_link_state cxl_link_state.c
 *   sudo ./cxl_link_state              # discover via CEDT
 *   sudo ./cxl_link_state 0xb3000000   # or name the RCRB base directly
 *
 * Read-only: opens /dev/mem O_RDONLY and maps PROT_READ. CONFIG_STRICT_DEVMEM
 * still permits this because an RCRB is MMIO, not RAM.
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define CEDT_PATH "/sys/firmware/acpi/tables/CEDT"

static const char *spd_name(unsigned v)
{
	switch (v) {
	case 1: return "2.5 GT/s (Gen1)";
	case 2: return "5 GT/s (Gen2)";
	case 3: return "8 GT/s (Gen3)";
	case 4: return "16 GT/s (Gen4)";
	case 5: return "32 GT/s (Gen5)";
	case 6: return "64 GT/s (Gen6)";
	}
	return "unknown";
}

/* Per-lane payload rate, GB/s, after 8b/10b or 128b/130b encoding. */
static double lane_gbps(unsigned s)
{
	switch (s) {
	case 1: return 0.250;
	case 2: return 0.500;
	case 3: return 0.985;
	case 4: return 1.969;
	case 5: return 3.938;
	case 6: return 7.877;
	}
	return 0;
}

/* Returns the number of CHBS entries printed; fills *out with the first base. */
static int cedt_find_rcrb(uint64_t *out)
{
	FILE *f = fopen(CEDT_PATH, "rb");
	if (!f) {
		perror("open " CEDT_PATH);
		return -1;
	}
	uint8_t buf[4096];
	size_t n = fread(buf, 1, sizeof(buf), f);
	fclose(f);
	if (n < 36) {
		fprintf(stderr, "CEDT too short (%zu bytes)\n", n);
		return -1;
	}
	uint32_t len;
	memcpy(&len, buf + 4, 4);
	if (len > n)
		len = (uint32_t)n;

	int found = 0;
	for (uint32_t off = 36; off + 4 <= len;) {
		uint8_t type = buf[off];
		uint16_t slen;
		memcpy(&slen, buf + off + 2, 2);
		if (slen < 4)
			break;
		if (type == 0 && off + 32 <= len) {		/* CHBS */
			uint32_t uid, ver;
			uint64_t base, blen;
			memcpy(&uid, buf + off + 4, 4);
			memcpy(&ver, buf + off + 8, 4);
			memcpy(&base, buf + off + 16, 8);
			memcpy(&blen, buf + off + 24, 8);
			printf("CEDT CHBS: uid=%u version=%u (%s) base=0x%" PRIx64 " len=0x%" PRIx64 "\n",
			       uid, ver, ver == 0 ? "CXL 1.1, RCRB" : "CXL 2.0, CHBCR", base, blen);
			if (ver != 0)
				printf("  note: CXL 2.0 host bridges expose a normal port hierarchy;\n"
				       "        read LnkSta from the port's config space instead.\n");
			if (!found++)
				*out = base;
		}
		off += slen;
	}
	if (!found)
		fprintf(stderr, "no CHBS entry in CEDT\n");
	return found;
}

int main(int argc, char **argv)
{
	uint64_t base = 0;
	if (argc > 1)
		base = strtoull(argv[1], NULL, 0);
	else if (cedt_find_rcrb(&base) <= 0)
		return 1;

	int fd = open("/dev/mem", O_RDONLY | O_SYNC);
	if (fd < 0) {
		perror("open /dev/mem (need root)");
		return 1;
	}
	volatile uint8_t *m = mmap(NULL, 0x2000, PROT_READ, MAP_SHARED, fd, (off_t)base);
	if (m == MAP_FAILED) {
		perror("mmap /dev/mem");
		return 1;
	}

	uint16_t vid = *(volatile uint16_t *)(m + 0x00);
	printf("RCRB @0x%" PRIx64 ": downstream port config space (vendor=0x%04x)\n", base, vid);

	uint32_t lnkcap = 0;
	uint16_t lnksta = 0;
	int found = 0;
	uint8_t cap = *(volatile uint8_t *)(m + 0x34);
	for (int guard = 0; cap && cap != 0xff && guard < 48; guard++) {
		uint8_t id = *(volatile uint8_t *)(m + cap);
		uint8_t next = *(volatile uint8_t *)(m + cap + 1);
		if (id == 0x10) {			/* PCI Express Capability */
			lnkcap = *(volatile uint32_t *)(m + cap + 0x0c);
			lnksta = *(volatile uint16_t *)(m + cap + 0x12);
			found = 1;
			break;
		}
		cap = next;
	}
	if (!found) {
		fprintf(stderr, "no PCIe capability (0x10) in the RCRB\n");
		return 2;
	}

	unsigned max_spd = lnkcap & 0xf, max_wid = (lnkcap >> 4) & 0x3f;
	unsigned cur_spd = lnksta & 0xf, cur_wid = (lnksta >> 4) & 0x3f;
	printf("  LnkCap 0x%08x  maximum   %-16s x%-2u  %5.1f GB/s\n",
	       lnkcap, spd_name(max_spd), max_wid, lane_gbps(max_spd) * max_wid);
	printf("  LnkSta 0x%04x      negotiated %-16s x%-2u  %5.1f GB/s\n",
	       lnksta, spd_name(cur_spd), cur_wid, lane_gbps(cur_spd) * cur_wid);

	putchar('\n');
	if (cur_spd < max_spd) {
		printf("  SPEED BELOW PORT MAXIMUM: running %s where the port supports %s.\n",
		       spd_name(cur_spd), spd_name(max_spd));
		printf("  This halves bandwidth per generation and is the first thing to check.\n");
	} else {
		printf("  speed is at the port maximum (%s)\n", spd_name(max_spd));
	}
	if (cur_wid < max_wid)
		printf("  width x%u of a x%u port -- compare against the DEVICE's width from its\n"
		       "  datasheet before calling this a fault. A x%u device in a x%u port\n"
		       "  negotiates x%u and that is correct; the port maximum describes the slot.\n",
		       cur_wid, max_wid, cur_wid, max_wid, cur_wid);
	else
		printf("  width is at the port maximum (x%u)\n", max_wid);
	return (cur_spd < max_spd) ? 3 : 0;
}
