/* error.h - minimal musl-portable replacement for glibc's <error.h>.
   Declares error()/error_at_line() and the error_message_count counter. */
#ifndef _ERROR_H
#define _ERROR_H 1

#include <stdarg.h>

#ifdef __cplusplus
extern "C" {
#endif

extern unsigned int error_message_count;

void error(int status, int errnum, const char *format, ...)
    __attribute__((format(printf, 3, 4)));

void error_at_line(int status, int errnum, const char *filename,
                   unsigned int line_number, const char *format, ...)
    __attribute__((format(printf, 5, 6)));

#ifdef __cplusplus
}
#endif

#endif /* _ERROR_H */