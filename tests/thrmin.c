/* CI 冒烟测试：pthread/mutex 最小回归（B-11 clone 修复的同类错误）
   通过条件：输出 THR_OK 且返回 0 */
#include <pthread.h>
#include <stdio.h>

#define NTHREADS 4
#define NITER    10000

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static long g_counter = 0;

static void* worker(void* arg)
{
    long i;
    (void)arg;
    for (i = 0; i < NITER; i++) {
        pthread_mutex_lock(&g_lock);
        g_counter++;
        pthread_mutex_unlock(&g_lock);
    }
    return NULL;
}

int main(void)
{
    pthread_t t[NTHREADS];
    int i, rc, fail = 0;

    for (i = 0; i < NTHREADS; i++) {
        rc = pthread_create(&t[i], NULL, worker, NULL);
        if (rc != 0) {
            printf("pthread_create FAIL i=%d rc=%d\n", i, rc);
            return 1;
        }
    }
    for (i = 0; i < NTHREADS; i++) {
        rc = pthread_join(t[i], NULL);
        if (rc != 0) {
            printf("pthread_join FAIL i=%d rc=%d\n", i, rc);
            fail = 1;
        }
    }

    if (g_counter != (long)NTHREADS * NITER) {
        printf("counter FAIL got=%ld want=%d\n", g_counter, NTHREADS * NITER);
        fail = 1;
    }

    if (fail) return 1;
    printf("THR_OK\n");
    return 0;
}
