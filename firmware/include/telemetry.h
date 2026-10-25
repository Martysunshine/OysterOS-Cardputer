// Resource telemetry (engineering plan): free heap, min free heap, largest free block, task stack
// high-water marks, queue occupancy, dropped events, reset reason. Implementation: telemetry.cpp.
// Emitted in the boot event and every cfg::kTelemetryPeriodMs. Goals are measured, never asserted.
#pragma once

#include <cstddef>
#include <cstdint>

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

namespace oyster {

class JsonWriter;

struct TaskStat {
  const char* name;
  uint32_t stack_hwm_bytes;
};

struct QueueStat {
  const char* name;
  uint16_t waiting;
  uint16_t depth;
  uint32_t dropped;
};

struct TelemetrySnapshot {
  uint32_t uptime_ms;
  uint32_t free_heap;
  uint32_t min_free_heap;
  uint32_t largest_free_block;
  int reset_reason;
  const char* reset_reason_str;
  TaskStat tasks[8];
  uint8_t task_count;
  QueueStat queues[8];
  uint8_t queue_count;
  bool heap_goal_met;
  bool largest_block_goal_met;
};

void telemetry_register_task(const char* name, TaskHandle_t h);
void telemetry_register_queue(const char* name, QueueHandle_t q, uint16_t depth, volatile uint32_t* dropped_counter);
void telemetry_snapshot(TelemetrySnapshot& out);
void telemetry_write_json(const TelemetrySnapshot& s, JsonWriter& w);  // members of the open object

}  // namespace oyster
