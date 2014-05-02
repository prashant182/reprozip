CC=gcc
LIBS=
CFLAGS=-W -Wall -Wextra -pedantic

OBJS=tracer.o

.PHONY: all clean

all: tracer

clean:
	rm $(OBJS)

tracer: $(OBJS)
	$(CC) $(CFLAGS) -o $@ $(OBJS)

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<
