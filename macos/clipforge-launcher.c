#include <unistd.h>

int main(void) {
    char *const args[] = {
        "/bin/zsh",
        "/Users/bene/Documents/ChatGPT/ClipForge/scripts/mac/clipforge-local.sh",
        "--start",
        NULL,
    };
    execv(args[0], args);
    return 127;
}
