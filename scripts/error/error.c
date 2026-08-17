/* error.c - minimal musl-portable implementation of glibc's error()/
   error_at_line(). Prints "<progname>: <msg>[: <strerror>]" to stderr and
   exits with STATUS if STATUS != 0. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <errno.h>

unsigned int error_message_count = 0;

static void
print_msg(int status, int errnum, const char *fmt, va_list ap)
{
    fprintf(stderr, "%s: ", program_invocation_name);
    vfprintf(stderr, fmt, ap);
    if (errnum)
        fprintf(stderr, ": %s", strerror(errnum));
    fputc('\n', stderr);
    ++error_message_count;
    if (status)
        exit(status);
}

void
error(int status, int errnum, const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    print_msg(status, errnum, format, ap);
    va_end(ap);
}

void
error_at_line(int status, int errnum, const char *filename,
              unsigned int line_number, const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    fprintf(stderr, "%s:", program_invocation_name);
    if (filename)
        fprintf(stderr, "%s:%u:", filename, line_number);
    fputc(' ', stderr);
    vfprintf(stderr, format, ap);
    if (errnum)
        fprintf(stderr, ": %s", strerror(errnum));
    fputc('\n', stderr);
    ++error_message_count;
    va_end(ap);
    if (status)
        exit(status);
}