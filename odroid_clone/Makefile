APP=clonador_sd
SRC=clonador_sd.c
HELPER=odroid_sd_helper.sh

CFLAGS += -O2 -Wall -Wextra
GTK_CFLAGS := $(shell pkg-config --cflags gtk+-3.0)
GTK_LIBS := $(shell pkg-config --libs gtk+-3.0)

.PHONY: all clean package

all: $(APP)

$(APP): $(SRC)
	gcc $(CFLAGS) $(GTK_CFLAGS) -o $@ $(SRC) $(GTK_LIBS) -lm
	chmod +x $(HELPER)

package: $(APP)
	mkdir -p dist
	cp $(APP) $(HELPER) dist/

clean:
	rm -f $(APP)
	rm -rf dist
