"""A small QR code generator (byte mode, error correction level M, versions 1-10).

Enough for the link the phone has to open, without any third-party package.
The algorithm follows the QR Code specification (ISO/IEC 18004).
"""

# Error correction level M: codewords per block and number of blocks, by version.
ECC_PER_BLOCK = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
BLOCKS = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
FORMAT_BITS_M = 0  # the two bits that identify level M in the format information

MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _raw_modules(version):
    """Modules available for data and error correction in a symbol of this version."""
    n = (16 * version + 128) * version + 64
    if version >= 2:
        align = version // 7 + 2
        n -= (25 * align - 10) * align - 55
        if version >= 7:
            n -= 36
    return n


def _data_capacity(version):
    """Data codewords (bytes) that fit, error correction excluded."""
    return _raw_modules(version) // 8 - ECC_PER_BLOCK[version] * BLOCKS[version]


def _alignment_positions(version):
    if version == 1:
        return []
    count = version // 7 + 2
    step = (version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
    size = version * 4 + 17
    return [6] + sorted(size - 7 - i * step for i in range(count - 1))


# ------------------------------------------------------------- Reed-Solomon

def _gf_mul(x, y):
    """Multiplication in GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _gf_mul(coefficient, factor)
    return result


def _add_ecc_and_interleave(data, version):
    blocks_count, ecc_len = BLOCKS[version], ECC_PER_BLOCK[version]
    raw = _raw_modules(version) // 8
    short_blocks = blocks_count - raw % blocks_count
    short_len = raw // blocks_count
    divisor = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(blocks_count):
        length = short_len - ecc_len + (0 if i < short_blocks else 1)
        chunk = data[k:k + length]
        k += length
        ecc = _rs_remainder(chunk, divisor)
        if i < short_blocks:
            chunk = chunk + [0]  # placeholder, skipped below
        blocks.append(chunk + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_len - ecc_len or j >= short_blocks:
                result.append(block[i])
    return result


# ------------------------------------------------------------------ symbol

class _Symbol:
    def __init__(self, version):
        self.version = version
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.reserved = [[False] * self.size for _ in range(self.size)]

    def set(self, x, y, dark):
        self.dark[y][x] = dark
        self.reserved[y][x] = True

    def draw_function_patterns(self):
        size = self.size
        for i in range(size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = _alignment_positions(self.version)
        last = len(positions) - 1
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue  # these corners hold the finder patterns
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        self.draw_version()

    def draw_format(self, mask):
        data = FORMAT_BITS_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        size = self.size
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, size - 15 + i, bit(i))
        self.set(8, size - 8, True)  # always dark

    def draw_version(self):
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            a, b = self.size - 11 + i % 3, i // 3
            self.set(a, b, dark)
            self.set(b, a, dark)

    def draw_codewords(self, codewords):
        size, i, total = self.size, 0, len(codewords) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5  # skip the vertical timing pattern
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = size - 1 - vert if upward else vert
                    if not self.reserved[y][x] and i < total:
                        self.dark[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        test = MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.reserved[y][x] and test(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self):
        size, score = self.size, 0
        lines = [''.join('1' if d else '0' for d in row) for row in self.dark]
        lines += [''.join(lines[y][x] for y in range(size)) for x in range(size)]
        for line in lines:
            run = 1
            for a, b in zip(line, line[1:]):
                if a == b:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2
            padded = '0000' + line + '0000'
            for pattern in ('10111010000', '00001011101'):
                start = padded.find(pattern)
                while start != -1:
                    score += 40
                    start = padded.find(pattern, start + 1)
        for y in range(size - 1):
            for x in range(size - 1):
                c = self.dark[y][x]
                if c == self.dark[y][x + 1] == self.dark[y + 1][x] == self.dark[y + 1][x + 1]:
                    score += 3
        dark = sum(row.count(True) for row in self.dark)
        total = size * size  # odd, so the balance is never exactly 50%
        score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
        return score


def matrix(text):
    """Returns the QR code of `text` as a list of rows of booleans (True = dark)."""
    data = text.encode('utf-8')
    for version in range(1, 11):
        count_bits = 8 if version < 10 else 16
        capacity = _data_capacity(version)
        if 4 + count_bits + len(data) * 8 <= capacity * 8:
            break
    else:
        raise ValueError('text too long for a QR code of version 10')

    bits = [0, 1, 0, 0]  # byte mode
    bits += [(len(data) >> i) & 1 for i in reversed(range(count_bits))]
    for byte in data:
        bits += [(byte >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    codewords = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(codewords) < capacity:
        codewords.append(pad)
        pad ^= 0xEC ^ 0x11

    best = None
    for mask in range(8):
        symbol = _Symbol(version)
        symbol.draw_function_patterns()
        symbol.draw_codewords(_add_ecc_and_interleave(codewords, version))
        symbol.apply_mask(mask)
        symbol.draw_format(mask)
        score = symbol.penalty()
        if best is None or score < best[0]:
            best = (score, symbol)
    return best[1].dark


def svg(text, border=2):
    """The QR code as an SVG image (black on white)."""
    rows = matrix(text)
    size = len(rows) + border * 2
    path = ''.join('M%d %dh1v1h-1z' % (x + border, y + border)
                   for y, row in enumerate(rows) for x, dark in enumerate(row) if dark)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" shape-rendering="crispEdges">'
            '<rect width="100%%" height="100%%" fill="#fff"/><path d="%s" fill="#000"/></svg>'
            % (size, size, path))


def terminal(text, border=2):
    """The QR code drawn with half blocks, two rows per line, black on white."""
    rows = matrix(text)
    width = len(rows) + border * 2
    blank = [False] * width
    rows = [blank] * border + [[False] * border + r + [False] * border for r in rows] + [blank] * border
    if len(rows) % 2:
        rows.append(blank)
    lines = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        chars = ''.join(' ▄▀█'[t * 2 + b] for t, b in zip(top, bottom))
        lines.append('\x1b[30;107m' + chars + '\x1b[0m')
    return '\n'.join(lines)
