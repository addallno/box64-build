/* obstack.h - minimal musl-portable obstack definitions
   Extracted from glibc obstack.h, trimmed to compile against musl. */

#ifndef _OBSTACK_H
#define _OBSTACK_H 1

#ifdef __PTRDIFF_TYPE__
# define PTR_INT_TYPE __PTRDIFF_TYPE__
#else
# include <stddef.h>
# define PTR_INT_TYPE ptrdiff_t
#endif

#define __BPTR_ALIGN(B, P, A) ((B) + (((P) - (B) + (A)) & ~(A)))

#define __PTR_ALIGN(B, P, A) \
  __BPTR_ALIGN (sizeof (PTR_INT_TYPE) < sizeof (void *) ? (B) : (char *) 0, P, A)

#include <string.h>

#ifndef __attribute_pure__
# define __attribute_pure__
#endif

#ifdef __cplusplus
extern "C" {
#endif

struct _obstack_chunk           /* Lives at front of each chunk. */
{
  char *limit;                  /* 1 past end of this chunk */
  struct _obstack_chunk *prev;  /* address of prior chunk or NULL */
  char contents[4];             /* objects begin here */
};

struct obstack          /* control current object in current chunk */
{
  long chunk_size;              /* preferred size to allocate chunks in */
  struct _obstack_chunk *chunk; /* address of current struct obstack_chunk */
  char *object_base;            /* address of object we are building */
  char *next_free;              /* where to add next char to current object */
  char *chunk_limit;            /* address of char after current chunk */
  union
  {
    PTR_INT_TYPE tempint;
    void *tempptr;
  } temp;                       /* Temporary for some macros.  */
  int alignment_mask;           /* Mask of alignment for each object. */
  struct _obstack_chunk *(*chunkfun) (void *, long);
  void (*freefun) (void *, struct _obstack_chunk *);
  void *extra_arg;              /* first arg for chunk alloc/dealloc funcs */
  unsigned use_extra_arg : 1;     /* chunk alloc/dealloc funcs take extra arg */
  unsigned maybe_empty_object : 1; /* The current chunk might contain a zero-length object.  */
  unsigned alloc_failed : 1;      /* No longer used, but retained for binary compatibility.  */
};

/* Declare the external functions we use; they are in obstack.c.  */

extern void _obstack_newchunk (struct obstack *, int);
extern int _obstack_begin (struct obstack *, int, int,
			   void *(*)(long), void (*)(void *));
extern int _obstack_begin_1 (struct obstack *, int, int,
			     void *(*)(void *, long),
			     void (*)(void *, void *), void *);
extern int _obstack_memory_used (struct obstack *) __attribute_pure__;

#ifndef __obstack_free
# define __obstack_free obstack_free
#endif
extern void __obstack_free (struct obstack *, void *);

extern void (*obstack_alloc_failed_handler) (void);

extern int obstack_exit_failure;

#include <stdarg.h>
extern int obstack_vprintf (struct obstack *, const char *, va_list);

#define obstack_base(h) ((void *) (h)->object_base)
#define obstack_chunk_size(h) ((h)->chunk_size)
#define obstack_next_free(h)    ((h)->next_free)
#define obstack_alignment_mask(h) ((h)->alignment_mask)

#define obstack_init(h) \
  _obstack_begin ((h), 0, 0, \
		  (void *(*)(long))obstack_chunk_alloc, \
		  (void (*)(void *))obstack_chunk_free)

#define obstack_begin(h, size) \
  _obstack_begin ((h), (size), 0, \
		  (void *(*)(long))obstack_chunk_alloc, \
		  (void (*)(void *))obstack_chunk_free)

#define obstack_specify_allocation(h, size, alignment, chunkfun, freefun) \
  _obstack_begin ((h), (size), (alignment), \
		  (void *(*)(long))(chunkfun), \
		  (void (*)(void *))(freefun))

#define obstack_specify_allocation_with_arg(h, size, alignment, chunkfun, freefun, arg) \
  _obstack_begin_1 ((h), (size), (alignment), \
		    (void *(*)(void *, long))(chunkfun), \
		    (void (*)(void *, void *))(freefun), (arg))

#define obstack_chunkfun(h, newchunkfun) \
  ((h)->chunkfun = (struct _obstack_chunk *(*)(void *, long))(newchunkfun))

#define obstack_freefun(h, newfreefun) \
  ((h)->freefun = (void (*)(void *, struct _obstack_chunk *))(newfreefun))

#define obstack_1grow_fast(h, achar) (*((h)->next_free)++ = (achar))
#define obstack_blank_fast(h, n) ((h)->next_free += (n))
#define obstack_memory_used(h) _obstack_memory_used (h)

#if defined __GNUC__ && __GNUC__ >= 2
# define obstack_object_size(OBSTACK) \
  __extension__ \
    ({ struct obstack const *__o = (OBSTACK); \
       (unsigned) (__o->next_free - __o->object_base); })

# define obstack_room(OBSTACK) \
  __extension__ \
    ({ struct obstack const *__o = (OBSTACK); \
       (unsigned) (__o->chunk_limit - __o->next_free); })

# define obstack_make_room(OBSTACK, length) \
  __extension__ \
    ({ struct obstack *__o = (OBSTACK); \
       int __len = (length); \
       if (__o->chunk_limit - __o->next_free < __len) \
	 _obstack_newchunk (__o, __len); \
       (void) 0; })

# define obstack_empty_p(OBSTACK) \
  __extension__ \
    ({ struct obstack const *__o = (OBSTACK); \
       (__o->chunk->prev == 0 \
	&& __o->next_free == __PTR_ALIGN ((char *) __o->chunk, \
					  __o->chunk->contents, \
					  __o->alignment_mask)); })

# define obstack_grow(OBSTACK, where, length) \
  __extension__ \
    ({ struct obstack *__o = (OBSTACK); \
       int __len = (length); \
       if (__o->chunk_limit - __o->next_free < __len) \
	 _obstack_newchunk (__o, __len); \
       memcpy (__o->next_free, where, __len); \
       __o->next_free += __len; \
       (void) 0; })

# define obstack_grow0(OBSTACK, where, length) \
  __extension__ \
    ({ struct obstack *__o = (OBSTACK); \
       int __len = (length); \
       if (__o->chunk_limit - __o->next_free < __len + 1) \
	 _obstack_newchunk (__o, __len + 1); \
       memcpy (__o->next_free, where, __len); \
       __o->next_free += __len; \
       *(__o->next_free)++ = 0; \
       (void) 0; })

# define obstack_1grow(OBSTACK, datum) \
  __extension__ \
    ({ struct obstack *__o = (OBSTACK); \
       if (__o->chunk_limit - __o->next_free < 1) \
	 _obstack_newchunk (__o, 1); \
       obstack_1grow_fast (__o, datum); \
       (void) 0; })

# define obstack_ptr_grow(OBSTACK, datum) \
  __extension__ \
    ({ struct obstack *__o = (OBSTACK); \
       if (__o->chunk_limit - __o->next_free < sizeof (void *) \
	   && ! (__o->chunk_limit - __o->next_free < 1 \
		 && __o->next_free == __PTR_ALIGN ((char *) __o->chunk, \
						   __o->chunk->contents, \
						   __o->alignment_mask))) \
	 _obstack_newchunk (__o, sizeof (void *)); \
       obstack_ptr_grow_fast (__o, datum); \
       (void) 0; })

# define obstack_int_grow(OBSTACK, datum) \
  __extension__ \
    ({ struct obstack *__o = (OBSTACK); \
       if (__o->chunk_limit - __o->next_free < sizeof (int) \
	   && ! (__o->chunk_limit - __o->next_free < 1 \
		 && __o->next_free == __PTR_ALIGN ((char *) __o->chunk, \
						   __o->chunk->contents, \
						   __o->alignment_mask))) \
	 _obstack_newchunk (__o, sizeof (int)); \
       obstack_int_grow_fast (__o, datum); \
       (void) 0; })

# define obstack_ptr_grow_fast(OBSTACK, aptr) \
  __extension__ \
    ({ struct obstack *__o1 = (OBSTACK); \
       *(const void **) __o1->next_free = (aptr); \
       __o1->next_free += sizeof (void *); \
       (void) 0; })

# define obstack_int_grow_fast(OBSTACK, aint) \
  __extension__ \
    ({ struct obstack *__o1 = (OBSTACK); \
       *(int *) __o1->next_free = (aint); \
       __o1->next_free += sizeof (int); \
       (void) 0; })

# define obstack_blank(OBSTACK, length) \
  __extension__ \
    ({ struct obstack *__o = (OBSTACK); \
       int __len = (length); \
       if (__o->chunk_limit - __o->next_free < __len) \
	 _obstack_newchunk (__o, __len); \
       obstack_blank_fast (__o, __len); \
       (void) 0; })

# define obstack_alloc(OBSTACK, length) \
  __extension__ \
    ({ struct obstack *__h = (OBSTACK); \
       obstack_blank (__h, (length)); \
       obstack_finish (__h); })

# define obstack_copy(OBSTACK, where, length) \
  __extension__ \
    ({ struct obstack *__h = (OBSTACK); \
       obstack_grow (__h, (where), (length)); \
       obstack_finish (__h); })

# define obstack_copy0(OBSTACK, where, length) \
  __extension__ \
    ({ struct obstack *__h = (OBSTACK); \
       obstack_grow0 (__h, (where), (length)); \
       obstack_finish (__h); })

# define obstack_finish(OBSTACK) \
  __extension__ \
    ({ struct obstack *__o = (OBSTACK); \
       void *__value = (void *) __o->object_base; \
       if (__o->next_free == __value) \
	 __o->maybe_empty_object = 1; \
       __o->next_free \
	 = __PTR_ALIGN (__o->object_base, __o->next_free, __o->alignment_mask); \
       if (__o->next_free - (char *) __o->chunk \
	   > __o->chunk_limit - (char *) __o->chunk) \
	 __o->next_free = __o->chunk_limit; \
       __o->object_base = __o->next_free; \
       __value; })

# define obstack_free(OBSTACK, OBJ) \
  __extension__ \
    ({ struct obstack *__o = (OBSTACK); \
       void *__obj = (void *) (OBJ); \
       if (__obj > (void *) __o->chunk && __obj < (void *) __o->chunk_limit) \
	 __o->next_free = __o->object_base = (char *) __obj; \
       else \
	 (__obstack_free) (__o, __obj); })

#else /* not __GNUC__ */

# define obstack_object_size(h) \
  ( (unsigned) ((h)->next_free - (h)->object_base) )
# define obstack_room(h) \
  ( (unsigned) ((h)->chunk_limit - (h)->next_free) )
# define obstack_empty_p(h) \
  ( (h)->chunk->prev == 0 \
    && (h)->next_free == __PTR_ALIGN ((char *) (h)->chunk, \
				      (h)->chunk->contents, \
				      (h)->alignment_mask) )
# define obstack_grow(h,where,length) \
  ( ((h)->next_free + (length) > (h)->chunk_limit \
     ? (_obstack_newchunk ((h), (length)), 0) : 0), \
    memcpy ((h)->next_free, where, length), \
    (h)->next_free += (length) )
# define obstack_grow0(h,where,length) \
  ( ((h)->next_free + (length) + 1 > (h)->chunk_limit \
     ? (_obstack_newchunk ((h), (length) + 1), 0) : 0), \
    memcpy ((h)->next_free, where, length), \
    (h)->next_free += (length), \
    *((h)->next_free)++ = 0 )
# define obstack_1grow(h,datum) \
  ( ((h)->next_free + 1 > (h)->chunk_limit \
     ? (_obstack_newchunk ((h), 1), 0) : 0), \
    obstack_1grow_fast (h, datum) )
# define obstack_ptr_grow(h,datum) \
  ( ((h)->next_free + sizeof (char *) > (h)->chunk_limit \
     ? (_obstack_newchunk ((h), sizeof (char *)), 0) : 0), \
    obstack_ptr_grow_fast (h, datum) )
# define obstack_int_grow(h,datum) \
  ( ((h)->next_free + sizeof (int) > (h)->chunk_limit \
     ? (_obstack_newchunk ((h), sizeof (int)), 0) : 0), \
    obstack_int_grow_fast (h, datum) )
# define obstack_ptr_grow_fast(h,aptr) \
  (((const void **) ((h)->next_free += sizeof (void *)))[-1] = (aptr))
# define obstack_int_grow_fast(h,aint) \
  (((int *) ((h)->next_free += sizeof (int)))[-1] = (aint))
# define obstack_blank(h,length) \
  ( ((h)->next_free + (length) > (h)->chunk_limit \
     ? (_obstack_newchunk ((h), (length)), 0) : 0), \
    obstack_blank_fast (h, (length)) )
# define obstack_alloc(h,length) \
  (obstack_blank ((h), (length)), obstack_finish ((h)))
# define obstack_copy(h,where,length) \
  (obstack_grow ((h), (where), (length)), obstack_finish ((h)))
# define obstack_copy0(h,where,length) \
  (obstack_grow0 ((h), (where), (length)), obstack_finish ((h)))
# define obstack_finish(h) \
  ( ((h)->next_free == (h)->object_base \
     ? (h)->maybe_empty_object = 1 \
     : 0), \
    (h)->next_free \
      = __PTR_ALIGN ((h)->object_base, (h)->next_free, (h)->alignment_mask), \
    (((h)->next_free - (char *) (h)->chunk \
      > (h)->chunk_limit - (char *) (h)->chunk) \
     ? (h)->next_free = (h)->chunk_limit : 0), \
    (h)->object_base = (h)->next_free, \
    (h)->temp.tempptr)
# define obstack_free(h,obj) \
  ( (h)->temp.tempint = (char *) (obj) - (char *) (h)->chunk, \
    ((((h)->temp.tempint > 0 \
       && (h)->temp.tempint < (h)->chunk_limit - (char *) (h)->chunk) \
      ? (h)->next_free = (h)->object_base = (h)->temp.tempint + (char *) (h)->chunk \
      : (((h)->next_free = (h)->object_base = (h)->temp.tempint + (char *) (h)->chunk), \
	  (__obstack_free) (h, (h)->temp.tempint + (char *) (h)->chunk))))

#endif /* not __GNUC__ */

#ifdef __cplusplus
}
#endif

#endif /* obstack.h */