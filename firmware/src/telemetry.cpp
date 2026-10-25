// Resource telemetry (telemetry.h). Heap figures are for internal 8-bit-capable RAM (no PSRAM on the ADV).
// Stack high-water marks are in bytes: ESP-IDF FreeRTOS uses an 8-bit StackType_t.
#include "telemetry.h"

#include <Arduino.h>
#include <esp_heap_caps.h>

#include "board_support.h"
#include "oyster_core/config.h"
#include "oyster_core/json_writer.h"

namespace oyster {

namespace {

constexpr uint8_t kMaxTasks = sizeof(TelemetrySnapshot::tasks) / sizeof(TelemetrySnapshot::tasks[0]);
constexpr uint8_t kMaxQueues = sizeof(TelemetrySnapshot::queues) / sizeof(TelemetrySnapshot::queues[0]);
constexpr uint32_t kHeapCaps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;

struct TaskReg {
  const char* name;
  TaskHandle_t handle;
};

struct QueueReg {
  const char* name;
  QueueHandle_t queue;
  uint16_t depth;
  volatile uint32_t* dropped;
};

TaskReg s_tasks[kMaxTasks] = {};
uint8_t s_task_count = 0;
QueueReg s_queues[kMaxQueues] = {};
uint8_t s_queue_count = 0;
portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

bool same_name(const char* a, const char* b) {
  if (a == b) return true;
  if (a == nullptr || b == nullptr) return false;
  while (*a && *a == *b) {
    ++a;
    ++b;
  }
  return *a == *b;
}

}  // namespace

// Names must be string literals (stored by pointer). Re-registering a name replaces its handle.
void telemetry_register_task(const char* name, TaskHandle_t h) {
  if (name == nullptr || h == nullptr) return;
  portENTER_CRITICAL(&s_mux);
  uint8_t i = 0;
  while (i < s_task_count && !same_name(s_tasks[i].name, name)) ++i;
  if (i < kMaxTasks) {
    s_tasks[i] = TaskReg{name, h};
    if (i == s_task_count) ++s_task_count;
  }
  portEXIT_CRITICAL(&s_mux);
}

void telemetry_register_queue(const char* name, QueueHandle_t q, uint16_t depth, volatile uint32_t* dropped_counter) {
  if (name == nullptr || q == nullptr) return;
  portENTER_CRITICAL(&s_mux);
  uint8_t i = 0;
  while (i < s_queue_count && !same_name(s_queues[i].name, name)) ++i;
  if (i < kMaxQueues) {
    s_queues[i] = QueueReg{name, q, depth, dropped_counter};
    if (i == s_queue_count) ++s_queue_count;
  }
  portEXIT_CRITICAL(&s_mux);
}

void telemetry_snapshot(TelemetrySnapshot& out) {
  out = TelemetrySnapshot{};
  out.uptime_ms = millis();
  out.free_heap = heap_caps_get_free_size(kHeapCaps);
  out.min_free_heap = heap_caps_get_minimum_free_size(kHeapCaps);
  out.largest_free_block = heap_caps_get_largest_free_block(kHeapCaps);
  out.reset_reason = g_board.info().reset_reason;
  out.reset_reason_str = g_board.info().reset_reason_str ? g_board.info().reset_reason_str : "unknown";

  // Copy the registries first; the FreeRTOS queries below must not run inside the critical section.
  TaskReg tasks[kMaxTasks];
  QueueReg queues[kMaxQueues];
  portENTER_CRITICAL(&s_mux);
  const uint8_t nt = s_task_count;
  const uint8_t nq = s_queue_count;
  for (uint8_t i = 0; i < nt; ++i) tasks[i] = s_tasks[i];
  for (uint8_t i = 0; i < nq; ++i) queues[i] = s_queues[i];
  portEXIT_CRITICAL(&s_mux);

  for (uint8_t i = 0; i < nt; ++i) {
    out.tasks[i].name = tasks[i].name;
    out.tasks[i].stack_hwm_bytes = static_cast<uint32_t>(uxTaskGetStackHighWaterMark(tasks[i].handle));
  }
  out.task_count = nt;
  for (uint8_t i = 0; i < nq; ++i) {
    out.queues[i].name = queues[i].name;
    out.queues[i].waiting = static_cast<uint16_t>(uxQueueMessagesWaiting(queues[i].queue));
    out.queues[i].depth = queues[i].depth;
    out.queues[i].dropped = queues[i].dropped ? *queues[i].dropped : 0;
  }
  out.queue_count = nq;

  out.heap_goal_met = out.min_free_heap >= cfg::kMinFreeHeapGoal;
  out.largest_block_goal_met = out.largest_free_block >= cfg::kMinLargestBlockGoal;
}

void telemetry_write_json(const TelemetrySnapshot& s, JsonWriter& w) {
  w.unum("uptime_ms", s.uptime_ms);
  w.unum("free_heap", s.free_heap);
  w.unum("min_free_heap", s.min_free_heap);
  w.unum("largest_free_block", s.largest_free_block);
  w.str("heap_caps", "internal_8bit");
  w.str("reset_reason", s.reset_reason_str);
  w.num("reset_reason_code", s.reset_reason);
  w.begin_array("tasks");
  for (uint8_t i = 0; i < s.task_count && i < kMaxTasks; ++i) {
    w.begin_object();
    w.str("name", s.tasks[i].name);
    w.unum("stack_hwm_bytes", s.tasks[i].stack_hwm_bytes);
    w.end_object();
  }
  w.end_array();
  w.begin_array("queues");
  for (uint8_t i = 0; i < s.queue_count && i < kMaxQueues; ++i) {
    w.begin_object();
    w.str("name", s.queues[i].name);
    w.unum("waiting", s.queues[i].waiting);
    w.unum("depth", s.queues[i].depth);
    w.unum("dropped", s.queues[i].dropped);
    w.end_object();
  }
  w.end_array();
  // Goals from the engineering plan: measured against, never asserted.
  w.begin_object("goals");
  w.unum("min_free_heap_goal", cfg::kMinFreeHeapGoal);
  w.boolean("min_free_heap_goal_met", s.heap_goal_met);
  w.unum("largest_block_goal", cfg::kMinLargestBlockGoal);
  w.boolean("largest_block_goal_met", s.largest_block_goal_met);
  w.end_object();
}

}  // namespace oyster
