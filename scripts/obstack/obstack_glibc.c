/* obstack_glibc.c - musl-portable glibc obstack implementation
   Extracted from glibc malloc/obstack.c, stripped of _LIBC/ELIDE/gettext.
   Provides: _obstack_begin, _obstack_begin_1, _obstack_newchunk,
             _obstack_allocated_p, obstack_free (__obstack_free),
             _obstack_memory_used, obstack_alloc_failed_handler,
             obstack_exit_failure, and obstack_vprintf (vsnprintf-based). */

#include <obstack.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>

#define OBSTACK_INTERFACE_VERSION 1

/* Determine default alignment.  */
union fooround
{
  uintmax_t i;
  long double d;
  void *p;
};
struct fooalign
{
  char c;
  union fooround u;
};
enum
{
  DEFAULT_ALIGNMENT = __builtin_offsetof (struct fooalign, u),
  DEFAULT_ROUNDING = sizeof (union fooround)
};

#ifndef COPYING_UNIT
# define COPYING_UNIT int
#endif

static void print_and_abort (void);
void (*obstack_alloc_failed_handler) (void) = print_and_abort;

int obstack_exit_failure = EXIT_FAILURE;

# define CALL_CHUNKFUN(h, size) \
  (((h)->use_extra_arg)							      \
   ? (*(h)->chunkfun)((h)->extra_arg, (size))				      \
   : (*(struct _obstack_chunk *(*)(long))(h)->chunkfun)((size)))

# define CALL_FREEFUN(h, old_chunk) \
  do { \
      if ((h)->use_extra_arg)						      \
	(*(h)->freefun)((h)->extra_arg, (old_chunk));			      \
      else								      \
	(*(void (*)(void *))(h)->freefun)((old_chunk));			      \
    } while (0)

int
_obstack_begin (struct obstack *h,
		int size, int alignment,
		void *(*chunkfun) (long),
		void (*freefun) (void *))
{
  struct _obstack_chunk *chunk;

  if (alignment == 0)
    alignment = DEFAULT_ALIGNMENT;
  if (size == 0)
    {
      int extra = ((((12 + DEFAULT_ROUNDING - 1) & ~(DEFAULT_ROUNDING - 1))
		    + 4 + DEFAULT_ROUNDING - 1)
		   & ~(DEFAULT_ROUNDING - 1));
      size = 4096 - extra;
    }

  h->chunkfun = (struct _obstack_chunk * (*) (void *, long)) chunkfun;
  h->freefun = (void (*) (void *, struct _obstack_chunk *)) freefun;
  h->chunk_size = size;
  h->alignment_mask = alignment - 1;
  h->use_extra_arg = 0;

  chunk = h->chunk = CALL_CHUNKFUN (h, h->chunk_size);
  if (!chunk)
    (*obstack_alloc_failed_handler) ();
  h->next_free = h->object_base = __PTR_ALIGN ((char *) chunk, chunk->contents,
					       alignment - 1);
  h->chunk_limit = chunk->limit
    = (char *) chunk + h->chunk_size;
  chunk->prev = NULL;
  h->maybe_empty_object = 0;
  h->alloc_failed = 0;
  return 1;
}

int
_obstack_begin_1 (struct obstack *h, int size, int alignment,
		  void *(*chunkfun) (void *, long),
		  void (*freefun) (void *, void *),
		  void *arg)
{
  struct _obstack_chunk *chunk;

  if (alignment == 0)
    alignment = DEFAULT_ALIGNMENT;
  if (size == 0)
    {
      int extra = ((((12 + DEFAULT_ROUNDING - 1) & ~(DEFAULT_ROUNDING - 1))
		    + 4 + DEFAULT_ROUNDING - 1)
		   & ~(DEFAULT_ROUNDING - 1));
      size = 4096 - extra;
    }

  h->chunkfun = (struct _obstack_chunk * (*)(void *,long)) chunkfun;
  h->freefun = (void (*) (void *, struct _obstack_chunk *)) freefun;
  h->chunk_size = size;
  h->alignment_mask = alignment - 1;
  h->extra_arg = arg;
  h->use_extra_arg = 1;

  chunk = h->chunk = CALL_CHUNKFUN (h, h->chunk_size);
  if (!chunk)
    (*obstack_alloc_failed_handler) ();
  h->next_free = h->object_base = __PTR_ALIGN ((char *) chunk, chunk->contents,
					       alignment - 1);
  h->chunk_limit = chunk->limit
    = (char *) chunk + h->chunk_size;
  chunk->prev = NULL;
  h->maybe_empty_object = 0;
  h->alloc_failed = 0;
  return 1;
}

void
_obstack_newchunk (struct obstack *h, int length)
{
  struct _obstack_chunk *old_chunk = h->chunk;
  struct _obstack_chunk *new_chunk;
  long new_size;
  long obj_size = h->next_free - h->object_base;
  long i;
  long already;
  char *object_base;

  new_size = (obj_size + length) + (obj_size >> 3) + h->alignment_mask + 100;
  if (new_size < h->chunk_size)
    new_size = h->chunk_size;

  new_chunk = CALL_CHUNKFUN (h, new_size);
  if (!new_chunk)
    (*obstack_alloc_failed_handler)();
  h->chunk = new_chunk;
  new_chunk->prev = old_chunk;
  new_chunk->limit = h->chunk_limit = (char *) new_chunk + new_size;

  object_base =
    __PTR_ALIGN ((char *) new_chunk, new_chunk->contents, h->alignment_mask);

  if (h->alignment_mask + 1 >= DEFAULT_ALIGNMENT)
    {
      for (i = obj_size / sizeof (COPYING_UNIT) - 1;
	   i >= 0; i--)
	((COPYING_UNIT *) object_base)[i]
	  = ((COPYING_UNIT *) h->object_base)[i];
      already = obj_size / sizeof (COPYING_UNIT) * sizeof (COPYING_UNIT);
    }
  else
    already = 0;
  for (i = already; i < obj_size; i++)
    object_base[i] = h->object_base[i];

  if (!h->maybe_empty_object
      && (h->object_base
	  == __PTR_ALIGN ((char *) old_chunk, old_chunk->contents,
			  h->alignment_mask)))
    {
      new_chunk->prev = old_chunk->prev;
      CALL_FREEFUN (h, old_chunk);
    }

  h->object_base = object_base;
  h->next_free = h->object_base + obj_size;
  h->maybe_empty_object = 0;
}

int _obstack_allocated_p (struct obstack *h, void *obj);

int
_obstack_allocated_p (struct obstack *h, void *obj)
{
  struct _obstack_chunk *lp;
  struct _obstack_chunk *plp;

  lp = (h)->chunk;
  while (lp != NULL && ((void *) lp >= obj || (void *) (lp)->limit < obj))
    {
      plp = lp->prev;
      lp = plp;
    }
  return lp != NULL;
}

# undef obstack_free

void
__obstack_free (struct obstack *h, void *obj)
{
  struct _obstack_chunk *lp;
  struct _obstack_chunk *plp;

  lp = h->chunk;
  while (lp != NULL && ((void *) lp >= obj || (void *) (lp)->limit < obj))
    {
      plp = lp->prev;
      CALL_FREEFUN (h, lp);
      lp = plp;
      h->maybe_empty_object = 1;
    }
  if (lp)
    {
      h->object_base = h->next_free = (char *) (obj);
      h->chunk_limit = lp->limit;
      h->chunk = lp;
    }
  else if (obj != NULL)
    abort ();
}

int
_obstack_memory_used (struct obstack *h)
{
  struct _obstack_chunk *lp;
  int nbytes = 0;

  for (lp = h->chunk; lp != NULL; lp = lp->prev)
    {
      nbytes += lp->limit - (char *) lp;
    }
  return nbytes;
}

static void
print_and_abort (void)
{
  fprintf (stderr, "%s\n", "memory exhausted");
  exit (obstack_exit_failure);
}

/* obstack_vprintf: vsnprintf-based replacement for glibc's printf_buffer
   implementation. Appends the formatted string to the end of the object
   currently being built in obstack OBSTACK, NUL-terminates it, and returns
   the number of bytes written (glibc semantics). */

int
obstack_vprintf (struct obstack *obstack, const char *fmt, va_list ap)
{
  va_list ap2;
  int len;

  va_copy (ap2, ap);
  len = vsnprintf (NULL, 0, fmt, ap2);
  va_end (ap2);
  if (len < 0)
    return len;

  obstack_grow (obstack, "", 0);  /* ensure a chunk exists */
  if (obstack_room (obstack) < len + 1)
    _obstack_newchunk (obstack, len + 1);

  vsnprintf (obstack_next_free (obstack), len + 1, fmt, ap);
  obstack->next_free += len;
  *obstack->next_free = '\0';
  return len;
}