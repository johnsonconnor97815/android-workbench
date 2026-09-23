#include <dlfcn.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static volatile sig_atomic_t stopping;

static void stop(int signum) {
    (void) signum;
    stopping = 1;
}

int main(int argc, char **argv) {
    void *module = NULL;
    signal(SIGTERM, stop);
    if (argc == 3) {
        module = dlopen(argv[1], RTLD_NOW);
        if (module == NULL) {
            fprintf(stderr, "dlopen: %s\n", dlerror());
            return 1;
        }
    } else if (argc != 2) {
        return 2;
    }
    printf("ready pid=%d\n", getpid());
    fflush(stdout);
    unsigned remaining = (unsigned) atoi(argv[argc - 1]);
    while (!stopping && remaining-- > 0)
        sleep(1);
    if (module != NULL) {
        if (dlclose(module) != 0) {
            fprintf(stderr, "dlclose: %s\n", dlerror());
            return 3;
        }
        puts("unloaded");
        fflush(stdout);
    }
    return 0;
}
