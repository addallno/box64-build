/* CI 冒烟测试：128/64 位除法回归（B-10 编译器 RT stub 被抢占的同类错误）
   通过条件：输出 DIV_OK 且返回 0 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

int main(void)
{
    int fail = 0;

    /* 1) u128: (2^86 + 0x3b7) / 2^22 == q=2^64, r=0x3b7 */
    {
        unsigned __int128 n = ((unsigned __int128)0x400000 << 64) + 0x3b7;
        unsigned __int128 q = n / (unsigned __int128)0x400000;
        unsigned __int128 r = n % (unsigned __int128)0x400000;
        if (q != ((unsigned __int128)1 << 64) || r != 0x3b7) {
            printf("u128_div FAIL q_lo=%llu q_hi=%llu r=%llu\n",
                   (unsigned long long)(uint64_t)q,
                   (unsigned long long)(uint64_t)(q >> 64),
                   (unsigned long long)r);
            fail = 1;
        }
    }

    /* 2) u64: (2^64-1) / (2^32-1) == q=0x100000001, r=0 */
    {
        uint64_t q = 0xFFFFFFFFFFFFFFFFull / 0xFFFFFFFFull;
        uint64_t r = 0xFFFFFFFFFFFFFFFFull % 0xFFFFFFFFull;
        if (q != 0x100000001ull || r != 0) {
            printf("u64_div FAIL q=%llu r=%llu\n",
                   (unsigned long long)q, (unsigned long long)r);
            fail = 1;
        }
    }

    /* 3) s128: 恒等式 q*3 + r == n 且 |r| < 3（触发有符号 128 位除法） */
    {
        signed __int128 n = ((signed __int128)5 << 100) - 12345;
        signed __int128 q = n / 3;
        signed __int128 r = n % 3;
        if (q * 3 + r != n || r <= -3 || r >= 3) {
            printf("s128_div FAIL r=%lld\n", (long long)r);
            fail = 1;
        }
    }

    /* 4) u128 混合小除数（编译器可能走 libgcc 常量除法路径） */
    {
        unsigned __int128 n = ((unsigned __int128)0xDEADBEEFCAFEBABEull << 64)
                              | 0x1122334455667788ull;
        unsigned __int128 q = n / 7;
        unsigned __int128 r = n % 7;
        if (q * 7 + r != n || r >= 7) {
            printf("u128_mod7 FAIL r=%llu\n", (unsigned long long)r);
            fail = 1;
        }
    }

    if (fail) return 1;
    printf("DIV_OK\n");
    return 0;
}
