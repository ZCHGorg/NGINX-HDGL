# HDGL v0.6 - Pure C Implementation Makefile
#
# Build targets:
#   make build          - Compile HDGL daemon
#   make clean          - Remove build artifacts
#   make test           - Run performance tests
#   make install        - Install daemon to /opt/hdgl/
#   make help           - Show this help

CC              = gcc
CFLAGS          = -Wall -Wextra -O3 -march=native -std=c11 -fPIC
LDFLAGS         = -lm -lcrypto -lssl -lpthread
DEBUG_FLAGS     = -g -O0 -DDEBUG

# Directories
SRCDIR           = src
INCDIR           = include
BINDIR           = bin
OBJDIR           = obj

# Source files
SOURCES         = $(SRCDIR)/hdgl_main.c \
                  $(SRCDIR)/hdgl_lattice.c \
				  $(SRCDIR)/hdgl_frame.c \
				  $(SRCDIR)/hdgl_http.c \
				  $(SRCDIR)/hdgl_transport.c

HEADERS         = $(INCDIR)/hdgl_core.h \
                  $(INCDIR)/hdgl_transport.h \
                  $(INCDIR)/hdgl_lattice.h

OBJECTS         = $(SOURCES:$(SRCDIR)/%.c=$(OBJDIR)/%.o)

# Targets
TARGET_DAEMON   = $(BINDIR)/hdgl_daemon
TARGET_TEST     = $(BINDIR)/hdgl_test

# ============================================================================
# Build Rules
# ============================================================================

.PHONY: all build clean install test help debug

all: build

build: $(BINDIR) $(OBJDIR) $(TARGET_DAEMON)
	@echo "✓ Build complete: $(TARGET_DAEMON)"
	@ls -lh $(TARGET_DAEMON)

$(BINDIR):
	@mkdir -p $(BINDIR)

$(OBJDIR):
	@mkdir -p $(OBJDIR)

$(TARGET_DAEMON): $(OBJECTS)
	$(CC) $(CFLAGS) -I$(INCDIR) -o $@ $^ $(LDFLAGS)
	@echo "✓ Linked: $@"

$(OBJDIR)/%.o: $(SRCDIR)/%.c $(HEADERS)
	$(CC) $(CFLAGS) -I$(INCDIR) -c -o $@ $<
	@echo "✓ Compiled: $<"

# ============================================================================
# Debug Build
# ============================================================================

debug: CFLAGS += $(DEBUG_FLAGS)
debug: clean build
	@echo "✓ Debug build complete"

# ============================================================================
# Testing
# ============================================================================

test: build
	@echo "Running performance tests..."
	@./$(TARGET_DAEMON) --test-frame-serialization
	@./$(TARGET_DAEMON) --test-phi-tau-routing
	@./$(TARGET_DAEMON) --test-connection-pool
	@echo "✓ All tests passed"

# ============================================================================
# Installation
# ============================================================================

install: build
	@echo "Installing HDGL v0.6 daemon..."
	@mkdir -p /opt/hdgl/bin
	@install -m 0755 $(TARGET_DAEMON) /opt/hdgl/bin/hdgl_daemon
	@mkdir -p /opt/hdgl_swap
	@mkdir -p /opt/hdgl_cache
	@echo "✓ Installation complete"
	@echo "Start daemon: /opt/hdgl/bin/hdgl_daemon"

# ============================================================================
# Utility Targets
# ============================================================================

clean:
	@rm -rf $(OBJDIR) $(BINDIR)
	@echo "✓ Cleaned build artifacts"

distclean: clean
	@rm -f *.o *.a $(TARGET_DAEMON) $(TARGET_TEST)
	@echo "✓ Clean distribution"

help:
	@echo "HDGL v0.6 - Pure C Implementation"
	@echo ""
	@echo "Build targets:"
	@echo "  make build          - Compile HDGL daemon"
	@echo "  make clean          - Remove build artifacts"
	@echo "  make debug          - Debug build (symbols, no optimization)"
	@echo "  make test           - Run performance tests"
	@echo "  make install        - Install daemon to /opt/hdgl/bin"
	@echo "  make distclean      - Remove all generated files"
	@echo "  make help           - Show this help"
	@echo ""
	@echo "Performance targets (v0.6):"
	@echo "  Throughput: 200K+ req/sec"
	@echo "  Latency P99: <1ms"
	@echo "  Memory: <10MB per 10K concurrent"
	@echo "  Concurrency: 500K+ connections"
	@echo ""
	@echo "Configuration:"
	@echo "  LN_LOCAL_NODE=<IP> - This node's IP address"
	@echo "  LN_CLUSTER_SECRET=<secret> - HMAC secret (must match on all nodes)"
	@echo "  LN_SIMULATION=1 - Simulation mode (dry-run)"
	@echo ""
	@echo "Example:"
	@echo "  make build"
	@echo "  LN_LOCAL_NODE=127.0.0.1 LN_CLUSTER_SECRET=mysecret /opt/hdgl/bin/hdgl_daemon"

# ============================================================================
# Profiling & Benchmarking
# ============================================================================

profile: CFLAGS += -pg
profile: clean build
	@echo "✓ Built with profiling support (-pg)"
	@echo "Run: gprof ./$(TARGET_DAEMON) gmon.out"

benchmark: build
	@echo "Running HDGL v0.6 benchmark suite..."
	@echo "  • Frame serialization (target: 125K ser/sec)"
	@echo "  • Phi-tau routing (target: O(1) with 90%+ cache hit)"
	@echo "  • Connection pool reuse (target: 96%+ reuse)"
	@echo "  • Strand routing (target: <1μs per lookup)"
	@echo ""
	@time -p ./$(TARGET_DAEMON) --benchmark
	@echo ""
	@echo "Benchmark complete. Expected improvement over v0.5: 4x throughput, 10x latency"

# ============================================================================
# Development Targets
# ============================================================================

format:
	@clang-format -i $(SOURCES) $(HEADERS)
	@echo "✓ Code formatted"

lint:
	@cppcheck --enable=all --suppress=missingIncludeSystem $(SRCDIR) $(INCDIR)
	@echo "✓ Static analysis complete"

valgrind: debug
	@echo "Running memory leak detection..."
	@valgrind --leak-check=full --show-leak-kinds=all \
		--track-origins=yes --verbose \
		./$(TARGET_DAEMON) --test-memory

# ============================================================================
# Dependency Information
# ============================================================================

info:
	@echo "HDGL v0.6 Build Configuration"
	@echo "=============================="
	@echo "Compiler: $(CC)"
	@echo "CFLAGS: $(CFLAGS)"
	@echo "LDFLAGS: $(LDFLAGS)"
	@echo ""
	@echo "Sources: $(SOURCES)"
	@echo "Headers: $(HEADERS)"
	@echo "Objects: $(OBJECTS)"
	@echo ""
	@echo "Required libraries:"
	@echo "  - libssl-dev (OpenSSL)"
	@echo "  - libcrypto (for HMAC-SHA256)"
	@echo ""
	@echo "Optional:"
	@echo "  - libuv (for event loop, currently using placeholders)"

.PRECIOUS: $(OBJDIR)/%.o
