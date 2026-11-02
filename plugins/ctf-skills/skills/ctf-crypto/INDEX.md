# ctf-crypto 索引

19 篇 · 合计 344K。**这张表是唯一目录来源**，入口 SKILL.md 不重复列举。

按体积降序。选题策略：先看「主题」是否命中症状，命中再读；一次最多读 2 篇，读完先动手验证再读下一篇。

| 文件 | 体积 | 主题（命中的技术名就是读取理由） |
|---|---|---|
| [rsa-attacks-2.md](rsa-attacks-2.md) | 36K | RSA p=q Validation Bypass (BearCatCTF 2026) / RSA with gcd(e, phi(n)) > 1 (CSAW 2015) / ROCA Attack on RSA CVE-2017-15361 (EasyCTF IV) |
| [modern-ciphers.md](modern-ciphers.md) | 32K | AES-CFB-8 Static IV State Forging / ECB Pattern Leakage on Images / Padding Oracle Attack / CBC-MAC vs OFB-MAC Vulnerability |
| [modern-ciphers-2.md](modern-ciphers-2.md) | 29K | Hash Length Extension Attack (PlaidCTF 2014) / Square Attack on Reduced-Round AES (0CTF 2016) |
| [classic-ciphers.md](classic-ciphers.md) | 28K | Vigenere Cipher / Atbash Cipher / Polybius Square Cipher (Qiwi-Infosec 2016) / Substitution Cipher with Rotating Wheel |
| [advanced-math.md](advanced-math.md) | 28K | Elliptic Curve Isogenies / Pohlig-Hellman Attack (Weak ECC) / Baby-Step Giant-Step for General DLP / LLL Algorithm for Approximate GCD |
| [prng.md](prng.md) | 25K | Mersenne Twister (MT19937) State Recovery / Time-Based Seed Attacks / C srand/rand Synchronization via Python ctypes / Layered Encryption Recovery |
| [exotic-crypto.md](exotic-crypto.md) | 24K | Monotone Function Inversion with Partial Output / Paillier Cryptosystem Attack (SECCON 2015) |
| [zkp-and-advanced.md](zkp-and-advanced.md) | 20K | ZKP Attacks / Graph 3-Coloring / Z3 SMT Solver Guide |
| [rsa-attacks.md](rsa-attacks.md) | 18K | Small Public Exponent (Cube Root) / Common Modulus Attack / Wiener's Attack (Small Private Exponent) / Pollard's p-1 Factorization |
| [exotic-crypto-2.md](exotic-crypto-2.md) | 18K | ElGamal Trivial DLP When B = p-1 (Hack.lu 2017) |
| [stream-ciphers.md](stream-ciphers.md) | 18K | LFSR Stream Cipher Attacks |
| [lattice-and-lwe.md](lattice-and-lwe.md) | 16K | Quick Triage: Is This a Lattice Problem? / Common Failure Modes / Quick Checklist Before You Commit to Lattices |
| [ecc-attacks.md](ecc-attacks.md) | 14K | Small Subgroup Attacks / Invalid Curve Attacks / Singular Curves / Smart's Attack (Anomalous Curves) |
| [prng-attacks.md](prng-attacks.md) | 12K | randcrack-Fed DSA k Prediction (CSAW CTF 2018) |
| [learned-20261028-8cb1ebff.md](learned-20261028-8cb1ebff.md) | 6K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261031-b26586b7.md](learned-20261031-b26586b7.md) | 5K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261031-9907ec4c.md](learned-20261031-9907ec4c.md) | 5K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261101-c944fced.md](learned-20261101-c944fced.md) | 5K | 什么时候用 / 怎么做 / 坑 / 已证伪的路线（别再走） |
| [historical.md](historical.md) | 5K | Lorenz SZ40/42 (Tunny) Cipher / Book Cipher Brute Force (Nullcon 2026) |

## 大文件（>25K，建议只 grep 片段而非整篇读入）

- `rsa-attacks-2.md` 36K
- `modern-ciphers.md` 32K
- `modern-ciphers-2.md` 29K
- `classic-ciphers.md` 28K
- `advanced-math.md` 28K
