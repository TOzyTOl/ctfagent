import sys


def check(s):
    if len(s) != 16:
        return False
    w = [ord(ch) for ch in s]
    if w[0] + w[3] - w[7] != 23: return False
    if 2*w[1] + w[15] - w[8] != 51: return False
    if (w[4] ^ 0x2a) - w[11] != -5: return False
    if (w[5] * w[12]) % 97 != 4: return False
    if w[2] + w[9] + w[13] - w[14] != 307: return False
    if (w[6] ^ 0x5c) - w[10] != 60: return False
    if w[9] - w[14] + w[3] != 119: return False
    if w[7] - w[6] + w[8] != 148: return False
    if w[0] - w[9] + w[7] != 101: return False
    if w[12] - w[14] + w[7] != 133: return False
    if w[12] - w[13] + w[4] != 38: return False
    if w[0] - w[1] + w[6] != 90: return False
    if w[3] - w[6] + w[13] != 118: return False
    if w[6] - w[3] + w[4] != 111: return False
    if w[10] - w[1] + w[8] != 84: return False
    if w[2] - w[6] + w[5] != 182: return False
    if w[8] - w[14] + w[7] != 166: return False
    return True


if __name__ == "__main__":
    s = input("口令> ").strip()
    print("正确。口令就是本题答案。" if check(s) else "不对。")
