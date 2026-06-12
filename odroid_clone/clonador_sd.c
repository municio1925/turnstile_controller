#include <errno.h>
#include <gtk/gtk.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>

#define PROVISIONING_URL "https://admin.fitnessmanagersystems.com/device/provisioning/reserve-remote-ssh-port/"
#define PROVISIONING_TOKEN "AEnmcK4a1s9GOYeGmW1uXoou2ltlyhA0-J-o3D4uB-8"
#define PKEXEC_PATH "/usr/bin/pkexec"

typedef enum {
  MODE_CLONE_AND_CONFIGURE = 0,
  MODE_CONFIGURE_ONLY = 1,
} OperationMode;

typedef struct {
  gchar *path;
  gchar *fstype;
  guint64 size_bytes;
} PartitionInfo;

typedef struct {
  gchar *name;
  gchar *path;
  gchar *model;
  gchar *transport;
  gboolean hotplug;
  gboolean removable;
  guint64 size_bytes;
  GPtrArray *partitions;
  gchar *display_label;
} DeviceInfo;

typedef struct {
  GtkWidget *window;
  GtkWidget *mode_combo;
  GtkWidget *source_combo;
  GtkWidget *target_combo;
  GtkWidget *refresh_button;
  GtkWidget *reserve_button;
  GtkWidget *eject_button;
  GtkWidget *start_button;
  GtkWidget *port_entry;
  GtkWidget *label_entry;
  GtkWidget *configure_check;
  GtkWidget *pairable_check;
  GtkWidget *progress_bar;
  GtkWidget *status_label;
  GtkWidget *detail_label;
  GtkWidget *log_view;
  GtkTextBuffer *log_buffer;
  GtkWidget *source_row;
  GtkWidget *helper_label;
  GPtrArray *devices;
  gchar *helper_path;
  gint reserved_port;
  gboolean busy;
} AppState;

typedef struct {
  AppState *app;
  gchar *message;
} LogMessageData;

typedef struct {
  AppState *app;
  gchar *status;
  gchar *detail;
  gdouble fraction;
  gboolean pulse;
} UiStatusData;

typedef struct {
  AppState *app;
  gboolean success;
  gint reserved_port;
  gchar *message;
} ReservePortResult;

typedef struct {
  AppState *app;
  gchar *label;
} ReservePortRequest;

typedef struct {
  AppState *app;
  gboolean success;
  gchar *message;
} OperationResult;

typedef struct {
  AppState *app;
  DeviceInfo *target;
} EjectRequest;

typedef struct {
  AppState *app;
  DeviceInfo *source;
  DeviceInfo *target;
  OperationMode mode;
  gboolean configure_port;
  gboolean prepare_pairing;
  gint reserved_port;
} OperationRequest;

static void device_info_free(gpointer data) {
  DeviceInfo *device = data;
  if (device == NULL) {
    return;
  }
  g_free(device->name);
  g_free(device->path);
  g_free(device->model);
  g_free(device->transport);
  g_free(device->display_label);
  if (device->partitions != NULL) {
    g_ptr_array_free(device->partitions, TRUE);
  }
  g_free(device);
}

static void partition_info_free(gpointer data) {
  PartitionInfo *partition = data;
  if (partition == NULL) {
    return;
  }
  g_free(partition->path);
  g_free(partition->fstype);
  g_free(partition);
}

static gchar *run_command_capture(const gchar *command, gint *exit_status, GError **error) {
  gchar *stdout_text = NULL;
  gchar *stderr_text = NULL;
  gint status = 0;
  gboolean ok = g_spawn_command_line_sync(command, &stdout_text, &stderr_text, &status, error);
  if (!ok) {
    g_free(stdout_text);
    g_free(stderr_text);
    return NULL;
  }

  if (exit_status != NULL) {
    if (WIFEXITED(status)) {
      *exit_status = WEXITSTATUS(status);
    } else {
      *exit_status = status;
    }
  }

  if (stderr_text != NULL && *stderr_text != '\0') {
    if (stdout_text == NULL || *stdout_text == '\0') {
      g_free(stdout_text);
      stdout_text = g_strdup(stderr_text);
    }
  }

  g_free(stderr_text);
  return stdout_text;
}

static gchar *parse_key_value(const gchar *line, const gchar *key) {
  gchar pattern[64];
  g_snprintf(pattern, sizeof(pattern), "%s=\"", key);
  gchar *start = g_strstr_len(line, -1, pattern);
  if (start == NULL) {
    return NULL;
  }
  start += strlen(pattern);
  gchar *end = strchr(start, '"');
  if (end == NULL) {
    return NULL;
  }
  return g_strndup(start, (gsize) (end - start));
}

static gchar *format_size(guint64 bytes) {
  const gchar *units[] = {"B", "KB", "MB", "GB", "TB"};
  gdouble size = (gdouble) bytes;
  guint unit_index = 0;
  while (size >= 1024.0 && unit_index < G_N_ELEMENTS(units) - 1) {
    size /= 1024.0;
    unit_index++;
  }
  return g_strdup_printf(unit_index == 0 ? "%.0f %s" : "%.1f %s", size, units[unit_index]);
}

static gchar *format_rate(gdouble bytes_per_second) {
  if (bytes_per_second <= 0.0) {
    return g_strdup("--");
  }
  return format_size((guint64) bytes_per_second);
}

static gchar *format_eta(gdouble seconds) {
  if (seconds <= 0.0 || !isfinite(seconds)) {
    return g_strdup("--");
  }
  guint total = (guint) llround(seconds);
  guint hours = total / 3600;
  guint minutes = (total % 3600) / 60;
  guint secs = total % 60;
  if (hours > 0) {
    return g_strdup_printf("%uh %02um %02us", hours, minutes, secs);
  }
  return g_strdup_printf("%um %02us", minutes, secs);
}

static gchar *json_escape(const gchar *value) {
  GString *escaped = g_string_new("");
  const gchar *cursor = value == NULL ? "" : value;
  while (*cursor != '\0') {
    switch (*cursor) {
      case '\\':
        g_string_append(escaped, "\\\\");
        break;
      case '"':
        g_string_append(escaped, "\\\"");
        break;
      case '\n':
        g_string_append(escaped, "\\n");
        break;
      case '\r':
        g_string_append(escaped, "\\r");
        break;
      case '\t':
        g_string_append(escaped, "\\t");
        break;
      default:
        g_string_append_c(escaped, *cursor);
        break;
    }
    cursor++;
  }
  return g_string_free(escaped, FALSE);
}

static gchar *extract_json_string(const gchar *json, const gchar *key) {
  gchar *pattern = g_strdup_printf("\"%s\":\"", key);
  gchar *start = g_strstr_len(json, -1, pattern);
  g_free(pattern);
  if (start == NULL) {
    return NULL;
  }
  start += strlen(key) + 4;
  gchar *end = strchr(start, '"');
  if (end == NULL) {
    return NULL;
  }
  return g_strndup(start, (gsize) (end - start));
}

static gint extract_json_int(const gchar *json, const gchar *key) {
  gchar *pattern = g_strdup_printf("\"%s\":", key);
  gchar *start = g_strstr_len(json, -1, pattern);
  g_free(pattern);
  if (start == NULL) {
    return -1;
  }
  start += strlen(key) + 3;
  while (*start == ' ' || *start == '\t') {
    start++;
  }
  return (gint) g_ascii_strtoll(start, NULL, 10);
}

static gboolean append_log_idle(gpointer user_data) {
  LogMessageData *payload = user_data;
  GtkTextIter end_iter;
  gtk_text_buffer_get_end_iter(payload->app->log_buffer, &end_iter);
  gtk_text_buffer_insert(payload->app->log_buffer, &end_iter, payload->message, -1);
  gtk_text_buffer_insert(payload->app->log_buffer, &end_iter, "\n", -1);
  GtkTextMark *mark = gtk_text_buffer_get_insert(payload->app->log_buffer);
  gtk_text_view_scroll_mark_onscreen(GTK_TEXT_VIEW(payload->app->log_view), mark);
  g_free(payload->message);
  g_free(payload);
  return G_SOURCE_REMOVE;
}

static void app_log(AppState *app, const gchar *format, ...) {
  va_list args;
  va_start(args, format);
  gchar *message = g_strdup_vprintf(format, args);
  va_end(args);

  LogMessageData *payload = g_new0(LogMessageData, 1);
  payload->app = app;
  payload->message = message;
  g_idle_add(append_log_idle, payload);
}

static gboolean update_status_idle(gpointer user_data) {
  UiStatusData *payload = user_data;
  gtk_label_set_text(GTK_LABEL(payload->app->status_label), payload->status);
  gtk_label_set_text(GTK_LABEL(payload->app->detail_label), payload->detail);
  if (payload->pulse) {
    gtk_progress_bar_pulse(GTK_PROGRESS_BAR(payload->app->progress_bar));
    gtk_progress_bar_set_show_text(GTK_PROGRESS_BAR(payload->app->progress_bar), FALSE);
  } else {
    gchar *progress_text = payload->fraction > 0.0 ? g_strdup_printf("%.1f%%", payload->fraction * 100.0) : g_strdup("");
    gtk_progress_bar_set_fraction(GTK_PROGRESS_BAR(payload->app->progress_bar), payload->fraction);
    gtk_progress_bar_set_text(GTK_PROGRESS_BAR(payload->app->progress_bar), progress_text);
    gtk_progress_bar_set_show_text(GTK_PROGRESS_BAR(payload->app->progress_bar), TRUE);
    g_free(progress_text);
  }
  g_free(payload->status);
  g_free(payload->detail);
  g_free(payload);
  return G_SOURCE_REMOVE;
}

static void set_status_async(AppState *app, gdouble fraction, gboolean pulse, const gchar *status, const gchar *detail) {
  UiStatusData *payload = g_new0(UiStatusData, 1);
  payload->app = app;
  payload->fraction = fraction;
  payload->pulse = pulse;
  payload->status = g_strdup(status);
  payload->detail = g_strdup(detail);
  g_idle_add(update_status_idle, payload);
}

static gchar *detect_root_disk_name(void) {
  gint status = 0;
  GError *error = NULL;
  gchar *root_source = run_command_capture("findmnt -n -o SOURCE /", &status, &error);
  if (error != NULL || status != 0 || root_source == NULL) {
    g_clear_error(&error);
    g_free(root_source);
    return NULL;
  }
  g_strstrip(root_source);
  gchar *command = g_strdup_printf("lsblk -no PKNAME %s", root_source);
  gchar *root_disk = run_command_capture(command, &status, &error);
  g_free(command);
  g_free(root_source);
  if (error != NULL || status != 0 || root_disk == NULL) {
    g_clear_error(&error);
    g_free(root_disk);
    return NULL;
  }
  g_strstrip(root_disk);
  return root_disk;
}

static gboolean is_candidate_disk(const gchar *name, const gchar *transport, gboolean hotplug, gboolean removable, const gchar *root_disk) {
  if (name == NULL || *name == '\0') {
    return FALSE;
  }
  if (root_disk != NULL && g_strcmp0(name, root_disk) == 0) {
    return FALSE;
  }
  if (g_str_has_prefix(name, "loop") || g_str_has_prefix(name, "zram") || g_str_has_prefix(name, "ram")) {
    return FALSE;
  }
  if (g_strcmp0(transport, "usb") == 0 || g_strcmp0(transport, "mmc") == 0) {
    return TRUE;
  }
  return hotplug || removable;
}

static GPtrArray *load_partitions_for_device(const gchar *device_path) {
  GPtrArray *partitions = g_ptr_array_new_with_free_func(partition_info_free);
  gchar *command = g_strdup_printf("lsblk -b -ln -P -o PATH,SIZE,FSTYPE,TYPE %s", device_path);
  GError *error = NULL;
  gint status = 0;
  gchar *output = run_command_capture(command, &status, &error);
  g_free(command);

  if (error != NULL || status != 0 || output == NULL) {
    g_clear_error(&error);
    g_free(output);
    return partitions;
  }

  gchar **lines = g_strsplit(output, "\n", -1);
  for (guint i = 0; lines[i] != NULL; i++) {
    gchar *line = g_strstrip(lines[i]);
    if (*line == '\0') {
      continue;
    }
    gchar *type = parse_key_value(line, "TYPE");
    if (g_strcmp0(type, "part") != 0) {
      g_free(type);
      continue;
    }
    PartitionInfo *partition = g_new0(PartitionInfo, 1);
    partition->path = parse_key_value(line, "PATH");
    partition->fstype = parse_key_value(line, "FSTYPE");
    gchar *size_text = parse_key_value(line, "SIZE");
    partition->size_bytes = size_text == NULL ? 0 : g_ascii_strtoull(size_text, NULL, 10);
    g_free(size_text);
    g_ptr_array_add(partitions, partition);
    g_free(type);
  }
  g_strfreev(lines);
  g_free(output);
  return partitions;
}

static GPtrArray *load_devices(void) {
  GPtrArray *devices = g_ptr_array_new_with_free_func(device_info_free);
  gchar *root_disk = detect_root_disk_name();
  GError *error = NULL;
  gint status = 0;
  gchar *output = run_command_capture("lsblk -b -dn -P -o NAME,PATH,SIZE,MODEL,TRAN,HOTPLUG,RM,TYPE", &status, &error);
  if (error != NULL || status != 0 || output == NULL) {
    g_clear_error(&error);
    g_free(output);
    g_free(root_disk);
    return devices;
  }

  gchar **lines = g_strsplit(output, "\n", -1);
  for (guint i = 0; lines[i] != NULL; i++) {
    gchar *line = g_strstrip(lines[i]);
    if (*line == '\0') {
      continue;
    }

    gchar *type = parse_key_value(line, "TYPE");
    if (g_strcmp0(type, "disk") != 0) {
      g_free(type);
      continue;
    }

    gchar *name = parse_key_value(line, "NAME");
    gchar *path = parse_key_value(line, "PATH");
    gchar *model = parse_key_value(line, "MODEL");
    gchar *transport = parse_key_value(line, "TRAN");
    gchar *hotplug_text = parse_key_value(line, "HOTPLUG");
    gchar *rm_text = parse_key_value(line, "RM");
    gchar *size_text = parse_key_value(line, "SIZE");

    gboolean hotplug = g_strcmp0(hotplug_text, "1") == 0;
    gboolean removable = g_strcmp0(rm_text, "1") == 0;
    if (!is_candidate_disk(name, transport, hotplug, removable, root_disk)) {
      g_free(name);
      g_free(path);
      g_free(model);
      g_free(transport);
      g_free(hotplug_text);
      g_free(rm_text);
      g_free(size_text);
      g_free(type);
      continue;
    }

    DeviceInfo *device = g_new0(DeviceInfo, 1);
    device->name = name;
    device->path = path;
    device->model = model == NULL ? g_strdup("") : model;
    device->transport = transport == NULL ? g_strdup("") : transport;
    device->hotplug = hotplug;
    device->removable = removable;
    device->size_bytes = size_text == NULL ? 0 : g_ascii_strtoull(size_text, NULL, 10);
    device->partitions = load_partitions_for_device(device->path);

    gchar *size_label = format_size(device->size_bytes);
    const gchar *model_text = (device->model != NULL && *device->model != '\0') ? device->model : "sin modelo";
    const gchar *transport_text = (device->transport != NULL && *device->transport != '\0') ? device->transport : "-";
    device->display_label = g_strdup_printf("%s | %s | %s | %s", device->path, size_label, model_text, transport_text);
    g_free(size_label);
    g_ptr_array_add(devices, device);

    g_free(hotplug_text);
    g_free(rm_text);
    g_free(size_text);
    g_free(type);
  }

  g_strfreev(lines);
  g_free(output);
  g_free(root_disk);
  return devices;
}

static void clear_combo(GtkComboBoxText *combo) {
  gtk_combo_box_text_remove_all(combo);
}

static DeviceInfo *get_selected_device(AppState *app, GtkWidget *combo_widget) {
  gint index = gtk_combo_box_get_active(GTK_COMBO_BOX(combo_widget));
  if (index < 0 || app->devices == NULL || (guint) index >= app->devices->len) {
    return NULL;
  }
  return g_ptr_array_index(app->devices, index);
}

static OperationMode current_mode(AppState *app) {
  gint index = gtk_combo_box_get_active(GTK_COMBO_BOX(app->mode_combo));
  return index == 1 ? MODE_CONFIGURE_ONLY : MODE_CLONE_AND_CONFIGURE;
}

static void update_ui_state(AppState *app) {
  OperationMode mode = current_mode(app);
  gboolean configure_port = gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->configure_check));
  gboolean prepare_pairing = gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->pairable_check));
  gboolean has_target = get_selected_device(app, app->target_combo) != NULL;
  gtk_widget_set_visible(app->source_row, mode == MODE_CLONE_AND_CONFIGURE);
  gtk_widget_set_sensitive(app->source_combo, !app->busy && mode == MODE_CLONE_AND_CONFIGURE);
  gtk_widget_set_sensitive(app->target_combo, !app->busy);
  gtk_widget_set_sensitive(app->mode_combo, !app->busy);
  gtk_widget_set_sensitive(app->refresh_button, !app->busy);
  gtk_widget_set_sensitive(app->configure_check, !app->busy && !prepare_pairing);
  gtk_widget_set_sensitive(app->pairable_check, !app->busy);
  gtk_widget_set_sensitive(app->label_entry, !app->busy && configure_port);
  gtk_widget_set_sensitive(app->reserve_button, !app->busy && configure_port);
  gtk_widget_set_sensitive(app->eject_button, !app->busy && has_target);
  gtk_widget_set_sensitive(app->start_button, !app->busy);
  gtk_widget_set_sensitive(app->port_entry, FALSE);
}

static void populate_device_combos(AppState *app) {
  clear_combo(GTK_COMBO_BOX_TEXT(app->source_combo));
  clear_combo(GTK_COMBO_BOX_TEXT(app->target_combo));

  if (app->devices != NULL) {
    g_ptr_array_free(app->devices, TRUE);
    app->devices = NULL;
  }

  app->devices = load_devices();
  for (guint i = 0; i < app->devices->len; i++) {
    DeviceInfo *device = g_ptr_array_index(app->devices, i);
    gtk_combo_box_text_append_text(GTK_COMBO_BOX_TEXT(app->source_combo), device->display_label);
    gtk_combo_box_text_append_text(GTK_COMBO_BOX_TEXT(app->target_combo), device->display_label);
  }
  if (app->devices->len > 0) {
    gtk_combo_box_set_active(GTK_COMBO_BOX(app->source_combo), 0);
    gtk_combo_box_set_active(GTK_COMBO_BOX(app->target_combo), app->devices->len > 1 ? 1 : 0);
  }
  app->reserved_port = 0;
  gtk_entry_set_text(GTK_ENTRY(app->port_entry), "");
  update_ui_state(app);
}

static gboolean finish_reserve_port_idle(gpointer user_data) {
  ReservePortResult *result = user_data;
  result->app->busy = FALSE;
  if (result->success) {
    result->app->reserved_port = result->reserved_port;
    gchar *port_text = g_strdup_printf("%d", result->reserved_port);
    gtk_entry_set_text(GTK_ENTRY(result->app->port_entry), port_text);
    g_free(port_text);
    gtk_label_set_text(
        GTK_LABEL(result->app->helper_label),
        "El puerto reservado y el DEVICE_IDENTIFIER derivado de ese puerto se grabaran en la tarjeta.");
    set_status_async(result->app, 0.0, FALSE, "Puerto reservado", result->message);
    app_log(result->app, "Puerto FRP reservado: %d", result->reserved_port);
  } else {
    set_status_async(result->app, 0.0, FALSE, "No se pudo reservar el puerto", result->message);
    app_log(result->app, "Error reservando puerto: %s", result->message);
  }
  update_ui_state(result->app);
  g_free(result->message);
  g_free(result);
  return G_SOURCE_REMOVE;
}

static gpointer reserve_port_thread(gpointer user_data) {
  ReservePortRequest *request = user_data;
  AppState *app = request->app;
  gchar *escaped_label = json_escape(request->label);
  gchar *json_payload = g_strdup_printf("{\"label\":\"%s\"}", escaped_label);
  gchar *quoted_json = g_shell_quote(json_payload);
  gchar *quoted_url = g_shell_quote(PROVISIONING_URL);
  gchar *quoted_token = g_shell_quote(PROVISIONING_TOKEN);
  gchar *command = g_strdup_printf(
      "curl -sS -X POST %s -H 'Content-Type: application/json' -H 'X-Device-Provisioning-Token: %s' -d %s",
      quoted_url,
      quoted_token,
      quoted_json);

  GError *error = NULL;
  gint status = 0;
  gchar *output = run_command_capture(command, &status, &error);

  ReservePortResult *result = g_new0(ReservePortResult, 1);
  result->app = app;

  if (error != NULL) {
    result->success = FALSE;
    result->message = g_strdup(error->message);
    g_clear_error(&error);
  } else if (status != 0 || output == NULL) {
    result->success = FALSE;
    result->message = g_strdup("El backend no respondio correctamente.");
  } else {
    gint port = extract_json_int(output, "ssh_port");
    if (port > 0) {
      result->success = TRUE;
      result->reserved_port = port;
      gchar *notes = extract_json_string(output, "notes");
      result->message = notes != NULL && *notes != '\0' ? notes : g_strdup("Puerto reservado correctamente.");
    } else {
      result->success = FALSE;
      result->message = g_strdup(output);
    }
  }

  g_free(output);
  g_free(command);
  g_free(quoted_json);
  g_free(quoted_url);
  g_free(quoted_token);
  g_free(json_payload);
  g_free(escaped_label);
  g_free(request->label);
  g_free(request);
  g_idle_add(finish_reserve_port_idle, result);
  return NULL;
}

static gchar *build_helper_path(void) {
  gchar *self_path = g_file_read_link("/proc/self/exe", NULL);
  if (self_path == NULL) {
    return g_build_filename(g_get_current_dir(), "odroid_sd_helper.sh", NULL);
  }
  gchar *directory = g_path_get_dirname(self_path);
  gchar *helper = g_build_filename(directory, "odroid_sd_helper.sh", NULL);
  g_free(directory);
  g_free(self_path);
  return helper;
}

static gboolean finish_operation_idle(gpointer user_data) {
  OperationResult *result = user_data;
  result->app->busy = FALSE;
  update_ui_state(result->app);
  if (result->success) {
    set_status_async(result->app, 1.0, FALSE, "Operacion completada", result->message);
    app_log(result->app, "Operacion completada: %s", result->message);
  } else {
    set_status_async(result->app, 0.0, FALSE, "Operacion fallida", result->message);
    app_log(result->app, "Operacion fallida: %s", result->message);
  }
  g_free(result->message);
  g_free(result);
  return G_SOURCE_REMOVE;
}

static void report_clone_progress(AppState *app, guint64 copied_bytes, guint64 total_bytes, gdouble elapsed_seconds) {
  gdouble fraction = total_bytes == 0 ? 0.0 : ((gdouble) copied_bytes / (gdouble) total_bytes);
  if (fraction > 1.0) {
    fraction = 1.0;
  }
  gdouble speed = elapsed_seconds > 0.0 ? ((gdouble) copied_bytes / elapsed_seconds) : 0.0;
  gdouble remaining = speed > 0.0 && total_bytes > copied_bytes ? ((gdouble) (total_bytes - copied_bytes) / speed) : 0.0;
  gchar *copied_text = format_size(copied_bytes);
  gchar *total_text = format_size(total_bytes);
  gchar *speed_text = format_rate(speed);
  gchar *eta_text = format_eta(remaining);
  gchar *detail = g_strdup_printf("%s de %s | %s/s | ETA %s", copied_text, total_text, speed_text, eta_text);
  set_status_async(app, fraction, FALSE, "Clonando la tarjeta...", detail);
  g_free(detail);
  g_free(copied_text);
  g_free(total_text);
  g_free(speed_text);
  g_free(eta_text);
}

static gboolean parse_progress_line(const gchar *line, guint64 *bytes_out) {
  if (line == NULL || !g_ascii_isdigit(line[0])) {
    return FALSE;
  }
  errno = 0;
  guint64 bytes = g_ascii_strtoull(line, NULL, 10);
  if (errno != 0 || bytes == 0) {
    return FALSE;
  }
  *bytes_out = bytes;
  return TRUE;
}

static gboolean run_clone_command(OperationRequest *request, gchar **error_message) {
  gchar *argv[] = {PKEXEC_PATH, request->app->helper_path, "clone", request->source->path, request->target->path, NULL};
  GError *error = NULL;
  GPid pid = 0;
  gint stdout_fd = -1;
  gint stderr_fd = -1;
  gint stdin_fd = -1;

  if (!g_spawn_async_with_pipes(NULL, argv, NULL, G_SPAWN_DO_NOT_REAP_CHILD, NULL, NULL, &pid, &stdin_fd, &stdout_fd,
                                &stderr_fd, &error)) {
    *error_message = g_strdup(error->message);
    g_clear_error(&error);
    return FALSE;
  }

  close(stdin_fd);
  close(stdout_fd);

  FILE *stream = fdopen(stderr_fd, "r");
  if (stream == NULL) {
    *error_message = g_strdup("No se pudo leer el progreso de clonacion.");
    close(stderr_fd);
    return FALSE;
  }

  gchar buffer[1024];
  gint64 started_at = g_get_monotonic_time();
  while (fgets(buffer, sizeof(buffer), stream) != NULL) {
    gchar *line = g_strstrip(buffer);
    guint64 copied_bytes = 0;
    if (parse_progress_line(line, &copied_bytes)) {
      gdouble elapsed = ((gdouble) (g_get_monotonic_time() - started_at)) / G_USEC_PER_SEC;
      report_clone_progress(request->app, copied_bytes, request->source->size_bytes, elapsed);
    } else if (*line != '\0') {
      app_log(request->app, "%s", line);
    }
  }

  fclose(stream);

  gint child_status = 0;
  waitpid(pid, &child_status, 0);
  g_spawn_close_pid(pid);

  if (!WIFEXITED(child_status) || WEXITSTATUS(child_status) != 0) {
    *error_message = g_strdup("La clonacion fallo o fue cancelada.");
    return FALSE;
  }
  return TRUE;
}

static gboolean run_configure_command(OperationRequest *request, const gchar *target_path, gchar **error_message) {
  gchar *port_text = g_strdup_printf("%d", request->reserved_port);
  gchar *argv[] = {PKEXEC_PATH, request->app->helper_path, "configure-port", (gchar *) target_path, port_text, NULL};
  GError *error = NULL;
  gint status = 0;
  gchar *stdout_text = NULL;
  gchar *stderr_text = NULL;

  gboolean ok = g_spawn_sync(NULL, argv, NULL, 0, NULL, NULL, &stdout_text, &stderr_text, &status, &error);
  g_free(port_text);

  if (!ok) {
    *error_message = g_strdup(error->message);
    g_clear_error(&error);
    g_free(stdout_text);
    g_free(stderr_text);
    return FALSE;
  }

  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    *error_message = stderr_text != NULL && *stderr_text != '\0' ? g_strdup(stderr_text)
                                                                 : g_strdup("No se pudo grabar el puerto en la tarjeta.");
    g_free(stdout_text);
    g_free(stderr_text);
    return FALSE;
  }

  if (stdout_text != NULL && *stdout_text != '\0') {
    app_log(request->app, "%s", stdout_text);
  }

  g_free(stdout_text);
  g_free(stderr_text);
  return TRUE;
}

static gboolean run_prepare_pairing_command(AppState *app, const gchar *target_path, gint reserved_port,
                                            gchar **error_message) {
  gchar *port_text = g_strdup_printf("%d", reserved_port);
  gchar *argv[] = {PKEXEC_PATH, app->helper_path, "prepare-pairing", (gchar *) target_path, port_text, NULL};
  GError *error = NULL;
  gint status = 0;
  gchar *stdout_text = NULL;
  gchar *stderr_text = NULL;

  gboolean ok = g_spawn_sync(NULL, argv, NULL, 0, NULL, NULL, &stdout_text, &stderr_text, &status, &error);
  g_free(port_text);

  if (!ok) {
    *error_message = g_strdup(error->message);
    g_clear_error(&error);
    g_free(stdout_text);
    g_free(stderr_text);
    return FALSE;
  }

  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    *error_message = stderr_text != NULL && *stderr_text != '\0' ? g_strdup(stderr_text)
                                                                 : g_strdup("No se pudo preparar la tarjeta para emparejar.");
    g_free(stdout_text);
    g_free(stderr_text);
    return FALSE;
  }

  if (stdout_text != NULL && *stdout_text != '\0') {
    app_log(app, "%s", stdout_text);
  }

  g_free(stdout_text);
  g_free(stderr_text);
  return TRUE;
}

static gboolean run_eject_command(AppState *app, const gchar *target_path, gchar **error_message) {
  gchar *argv[] = {PKEXEC_PATH, app->helper_path, "eject-card", (gchar *) target_path, NULL};
  GError *error = NULL;
  gint status = 0;
  gchar *stdout_text = NULL;
  gchar *stderr_text = NULL;

  gboolean ok = g_spawn_sync(NULL, argv, NULL, 0, NULL, NULL, &stdout_text, &stderr_text, &status, &error);

  if (!ok) {
    *error_message = g_strdup(error->message);
    g_clear_error(&error);
    g_free(stdout_text);
    g_free(stderr_text);
    return FALSE;
  }

  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    *error_message = stderr_text != NULL && *stderr_text != '\0' ? g_strdup(stderr_text)
                                                                 : g_strdup("No se pudo expulsar la tarjeta.");
    g_free(stdout_text);
    g_free(stderr_text);
    return FALSE;
  }

  if (stdout_text != NULL && *stdout_text != '\0') {
    app_log(app, "%s", stdout_text);
  }

  g_free(stdout_text);
  g_free(stderr_text);
  return TRUE;
}

static gpointer operation_thread(gpointer user_data) {
  OperationRequest *request = user_data;
  OperationResult *result = g_new0(OperationResult, 1);
  result->app = request->app;
  result->success = FALSE;

  if (request->mode == MODE_CLONE_AND_CONFIGURE) {
    app_log(request->app, "Clonando %s en %s", request->source->path, request->target->path);
    set_status_async(request->app, 0.0, FALSE, "Clonando la tarjeta...", "Preparando dispositivos...");
    gchar *error_message = NULL;
    if (!run_clone_command(request, &error_message)) {
      result->message = error_message;
      g_idle_add(finish_operation_idle, result);
      g_free(request);
      return NULL;
    }
  }

  if (request->configure_port) {
    gchar *detail = g_strdup_printf("Aplicando el puerto %d en %s", request->reserved_port, request->target->path);
    set_status_async(request->app, 0.0, TRUE, "Grabando el puerto FRP...", detail);
    g_free(detail);
    app_log(request->app, "Grabando puerto %d en %s", request->reserved_port, request->target->path);

    gchar *error_message = NULL;
    if (!run_configure_command(request, request->target->path, &error_message)) {
      result->message = error_message;
      g_idle_add(finish_operation_idle, result);
      g_free(request);
      return NULL;
    }
  }

  if (request->prepare_pairing) {
    gchar *detail = g_strdup_printf(
        "Limpiando gimnasio y entradas, y fijando el DEVICE_IDENTIFIER ligado al puerto %d.", request->reserved_port);
    set_status_async(request->app, 0.0, TRUE, "Preparando para emparejar...", detail);
    g_free(detail);
    app_log(request->app, "Limpiando ajustes de emparejamiento y grabando identidad de tarjeta en %s", request->target->path);

    gchar *error_message = NULL;
    if (!run_prepare_pairing_command(request->app, request->target->path, request->reserved_port, &error_message)) {
      result->message = error_message;
      g_idle_add(finish_operation_idle, result);
      g_free(request);
      return NULL;
    }
  }

  result->success = TRUE;
  if (request->configure_port && request->prepare_pairing) {
    result->message = g_strdup_printf(
        "Puerto %d, DEVICE_IDENTIFIER y estado emparejable grabados correctamente.", request->reserved_port);
  } else if (request->configure_port) {
    result->message = g_strdup_printf("Puerto %d y DEVICE_IDENTIFIER grabados correctamente en la tarjeta.", request->reserved_port);
  } else if (request->prepare_pairing) {
    result->message = g_strdup("La tarjeta se preparo correctamente para arrancar en estado emparejable.");
  } else {
    result->message = g_strdup("Clonacion completada sin cambios de configuracion.");
  }
  g_idle_add(finish_operation_idle, result);
  g_free(request);
  return NULL;
}

static gpointer eject_thread(gpointer user_data) {
  EjectRequest *request = user_data;
  OperationResult *result = g_new0(OperationResult, 1);
  result->app = request->app;
  result->success = FALSE;

  gchar *error_message = NULL;
  if (!run_eject_command(request->app, request->target->path, &error_message)) {
    result->message = error_message;
    g_idle_add(finish_operation_idle, result);
    g_free(request);
    return NULL;
  }

  result->success = TRUE;
  result->message = g_strdup_printf("La tarjeta %s ya se puede retirar con seguridad.", request->target->path);
  g_idle_add(finish_operation_idle, result);
  g_free(request);
  return NULL;
}

static void on_refresh_clicked(GtkButton *button, gpointer user_data) {
  (void) button;
  AppState *app = user_data;
  populate_device_combos(app);
  app_log(app, "Lista de tarjetas actualizada.");
}

static void on_mode_changed(GtkComboBox *combo, gpointer user_data) {
  (void) combo;
  AppState *app = user_data;
  update_ui_state(app);
}

static void on_configure_toggled(GtkToggleButton *toggle, gpointer user_data) {
  AppState *app = user_data;
  if (toggle == GTK_TOGGLE_BUTTON(app->pairable_check) &&
      gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->pairable_check)) &&
      !gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->configure_check))) {
    gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(app->configure_check), TRUE);
  }

  gboolean configure_port = gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->configure_check));
  gboolean prepare_pairing = gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->pairable_check));
  if (prepare_pairing) {
    gtk_label_set_text(GTK_LABEL(app->helper_label),
                       "Se grabaran el puerto FRP y el DEVICE_IDENTIFIER sincronizado con la tarjeta, y se limpiaran gimnasio y entradas.");
  } else if (configure_port) {
    gtk_label_set_text(GTK_LABEL(app->helper_label),
                       "El puerto reservado y el DEVICE_IDENTIFIER derivado de ese puerto se grabaran en la tarjeta.");
  } else {
    gtk_label_set_text(GTK_LABEL(app->helper_label),
                       "Activa una opcion de configuracion para modificar la tarjeta.");
  }
  update_ui_state(app);
}

static void on_reserve_clicked(GtkButton *button, gpointer user_data) {
  (void) button;
  AppState *app = user_data;
  if (!gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->configure_check))) {
    set_status_async(app, 0.0, FALSE, "Sin configuracion", "Activa la opcion de grabar puerto FRP primero.");
    return;
  }
  DeviceInfo *target = get_selected_device(app, app->target_combo);
  if (target == NULL) {
    set_status_async(app, 0.0, FALSE, "Falta el destino", "Selecciona primero la tarjeta destino.");
    return;
  }
  app->busy = TRUE;
  update_ui_state(app);
  set_status_async(app, 0.0, TRUE, "Reservando puerto FRP...", "Consultando el backend de produccion...");
  app_log(app, "Reservando un nuevo puerto FRP para la tarjeta destino.");
  ReservePortRequest *request = g_new0(ReservePortRequest, 1);
  request->app = app;
  request->label = g_strdup(gtk_entry_get_text(GTK_ENTRY(app->label_entry)));
  g_thread_new("reserve-port-thread", reserve_port_thread, request);
}

static void on_start_clicked(GtkButton *button, gpointer user_data) {
  (void) button;
  AppState *app = user_data;
  OperationMode mode = current_mode(app);
  gboolean configure_port = gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->configure_check));
  gboolean prepare_pairing = gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(app->pairable_check));
  DeviceInfo *target = get_selected_device(app, app->target_combo);
  DeviceInfo *source = get_selected_device(app, app->source_combo);

  if (target == NULL) {
    set_status_async(app, 0.0, FALSE, "Falta el destino", "Selecciona una tarjeta destino.");
    return;
  }
  if (mode == MODE_CLONE_AND_CONFIGURE) {
    if (source == NULL) {
      set_status_async(app, 0.0, FALSE, "Falta el origen", "Selecciona una tarjeta origen.");
      return;
    }
    if (g_strcmp0(source->path, target->path) == 0) {
      set_status_async(app, 0.0, FALSE, "Seleccion invalida", "El origen y el destino no pueden ser la misma tarjeta.");
      return;
    }
  }
  if (configure_port && app->reserved_port <= 0) {
    set_status_async(app, 0.0, FALSE, "Falta el puerto reservado", "Reserva primero el puerto que se grabara en la tarjeta.");
    return;
  }
  if (prepare_pairing && app->reserved_port <= 0) {
    set_status_async(app, 0.0, FALSE, "Falta el puerto reservado",
                     "Para preparar la tarjeta y darle un identificador unico, primero hay que reservar un puerto.");
    return;
  }
  if (!configure_port && !prepare_pairing && mode == MODE_CONFIGURE_ONLY) {
    set_status_async(app, 0.0, FALSE, "Nada que aplicar", "Activa al menos una opcion de configuracion.");
    return;
  }

  OperationRequest *request = g_new0(OperationRequest, 1);
  request->app = app;
  request->source = source;
  request->target = target;
  request->mode = mode;
  request->configure_port = configure_port;
  request->prepare_pairing = prepare_pairing;
  request->reserved_port = app->reserved_port;

  app->busy = TRUE;
  update_ui_state(app);
  gtk_progress_bar_set_fraction(GTK_PROGRESS_BAR(app->progress_bar), 0.0);
  gtk_progress_bar_set_show_text(GTK_PROGRESS_BAR(app->progress_bar), TRUE);
  gtk_progress_bar_set_text(GTK_PROGRESS_BAR(app->progress_bar), "");
  app_log(app, "Operacion iniciada: %s", mode == MODE_CLONE_AND_CONFIGURE ? "clonar y configurar" : "solo configurar");
  g_thread_new("operation-thread", operation_thread, request);
}

static void on_eject_clicked(GtkButton *button, gpointer user_data) {
  (void) button;
  AppState *app = user_data;
  DeviceInfo *target = get_selected_device(app, app->target_combo);
  if (target == NULL) {
    set_status_async(app, 0.0, FALSE, "Falta el destino", "Selecciona una tarjeta para expulsarla.");
    return;
  }

  app->busy = TRUE;
  update_ui_state(app);
  set_status_async(app, 0.0, TRUE, "Expulsando la tarjeta...", "Vaciando buffers y desmontando la tarjeta.");
  app_log(app, "Expulsando de forma segura %s", target->path);

  EjectRequest *request = g_new0(EjectRequest, 1);
  request->app = app;
  request->target = target;
  g_thread_new("eject-thread", eject_thread, request);
}

static GtkWidget *create_labeled_row(const gchar *label_text, GtkWidget *child) {
  GtkWidget *row = gtk_box_new(GTK_ORIENTATION_VERTICAL, 6);
  GtkWidget *label = gtk_label_new(label_text);
  gtk_widget_set_halign(label, GTK_ALIGN_START);
  gtk_box_pack_start(GTK_BOX(row), label, FALSE, FALSE, 0);
  gtk_box_pack_start(GTK_BOX(row), child, FALSE, FALSE, 0);
  return row;
}

int main(int argc, char **argv) {
  gtk_init(&argc, &argv);

  AppState *app = g_new0(AppState, 1);
  app->helper_path = build_helper_path();

  app->window = gtk_window_new(GTK_WINDOW_TOPLEVEL);
  gtk_window_set_title(GTK_WINDOW(app->window), "Clonador de tarjetas Odroid");
  gtk_window_set_default_size(GTK_WINDOW(app->window), 980, 760);
  gtk_container_set_border_width(GTK_CONTAINER(app->window), 18);
  g_signal_connect(app->window, "destroy", G_CALLBACK(gtk_main_quit), NULL);

  GtkWidget *root = gtk_box_new(GTK_ORIENTATION_VERTICAL, 16);
  gtk_container_add(GTK_CONTAINER(app->window), root);

  GtkWidget *title = gtk_label_new(NULL);
  gtk_label_set_markup(GTK_LABEL(title), "<span size='xx-large' weight='bold'>Clonador de tarjetas Odroid</span>");
  gtk_widget_set_halign(title, GTK_ALIGN_START);
  gtk_box_pack_start(GTK_BOX(root), title, FALSE, FALSE, 0);

  GtkWidget *subtitle = gtk_label_new(
      "Clona micro SDs, reserva un puerto FRP unico y graba en la tarjeta tanto ese puerto como el DEVICE_IDENTIFIER asociado.");
  gtk_widget_set_halign(subtitle, GTK_ALIGN_START);
  gtk_label_set_line_wrap(GTK_LABEL(subtitle), TRUE);
  gtk_box_pack_start(GTK_BOX(root), subtitle, FALSE, FALSE, 0);

  GtkWidget *grid = gtk_grid_new();
  gtk_grid_set_row_spacing(GTK_GRID(grid), 14);
  gtk_grid_set_column_spacing(GTK_GRID(grid), 18);
  gtk_box_pack_start(GTK_BOX(root), grid, FALSE, FALSE, 0);

  app->mode_combo = gtk_combo_box_text_new();
  gtk_combo_box_text_append_text(GTK_COMBO_BOX_TEXT(app->mode_combo), "Clonar y configurar");
  gtk_combo_box_text_append_text(GTK_COMBO_BOX_TEXT(app->mode_combo), "Solo configurar tarjeta");
  gtk_combo_box_set_active(GTK_COMBO_BOX(app->mode_combo), 0);
  g_signal_connect(app->mode_combo, "changed", G_CALLBACK(on_mode_changed), app);
  gtk_grid_attach(GTK_GRID(grid), create_labeled_row("Modo de trabajo", app->mode_combo), 0, 0, 1, 1);

  app->refresh_button = gtk_button_new_with_label("Actualizar tarjetas");
  g_signal_connect(app->refresh_button, "clicked", G_CALLBACK(on_refresh_clicked), app);
  gtk_grid_attach(GTK_GRID(grid), app->refresh_button, 1, 0, 1, 1);

  app->source_combo = gtk_combo_box_text_new();
  app->source_row = create_labeled_row("Tarjeta origen", app->source_combo);
  gtk_grid_attach(GTK_GRID(grid), app->source_row, 0, 1, 2, 1);

  app->target_combo = gtk_combo_box_text_new();
  gtk_grid_attach(GTK_GRID(grid), create_labeled_row("Tarjeta destino", app->target_combo), 0, 2, 2, 1);

  GtkWidget *config_frame = gtk_frame_new("Opciones de configuracion");
  gtk_box_pack_start(GTK_BOX(root), config_frame, FALSE, FALSE, 0);
  GtkWidget *config_box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 12);
  gtk_container_set_border_width(GTK_CONTAINER(config_box), 14);
  gtk_container_add(GTK_CONTAINER(config_frame), config_box);

  app->configure_check = gtk_check_button_new_with_label("Grabar el puerto FRP remoto en la tarjeta");
  gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(app->configure_check), TRUE);
  g_signal_connect(app->configure_check, "toggled", G_CALLBACK(on_configure_toggled), app);
  gtk_box_pack_start(GTK_BOX(config_box), app->configure_check, FALSE, FALSE, 0);

  app->pairable_check = gtk_check_button_new_with_label("Preparar la tarjeta para emparejar de nuevo");
  gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(app->pairable_check), TRUE);
  g_signal_connect(app->pairable_check, "toggled", G_CALLBACK(on_configure_toggled), app);
  gtk_box_pack_start(GTK_BOX(config_box), app->pairable_check, FALSE, FALSE, 0);

  app->label_entry = gtk_entry_new();
  gtk_entry_set_placeholder_text(GTK_ENTRY(app->label_entry), "Etiqueta opcional para la reserva");
  gtk_box_pack_start(GTK_BOX(config_box), create_labeled_row("Etiqueta de la reserva", app->label_entry), FALSE, FALSE, 0);

  GtkWidget *port_row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 12);
  app->port_entry = gtk_entry_new();
  gtk_editable_set_editable(GTK_EDITABLE(app->port_entry), FALSE);
  gtk_widget_set_hexpand(app->port_entry, TRUE);
  gtk_box_pack_start(GTK_BOX(port_row), create_labeled_row("Puerto FRP reservado", app->port_entry), TRUE, TRUE, 0);
  app->reserve_button = gtk_button_new_with_label("Reservar puerto");
  g_signal_connect(app->reserve_button, "clicked", G_CALLBACK(on_reserve_clicked), app);
  gtk_box_pack_start(GTK_BOX(port_row), app->reserve_button, FALSE, FALSE, 0);
  gtk_box_pack_start(GTK_BOX(config_box), port_row, FALSE, FALSE, 0);

  app->helper_label = gtk_label_new("Reserva el puerto antes de empezar para saber exactamente que puerto y que DEVICE_IDENTIFIER se grabaran.");
  gtk_widget_set_halign(app->helper_label, GTK_ALIGN_START);
  gtk_label_set_line_wrap(GTK_LABEL(app->helper_label), TRUE);
  gtk_box_pack_start(GTK_BOX(config_box), app->helper_label, FALSE, FALSE, 0);

  GtkWidget *progress_frame = gtk_frame_new("Progreso");
  gtk_box_pack_start(GTK_BOX(root), progress_frame, FALSE, FALSE, 0);
  GtkWidget *progress_box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 10);
  gtk_container_set_border_width(GTK_CONTAINER(progress_box), 14);
  gtk_container_add(GTK_CONTAINER(progress_frame), progress_box);

  app->status_label = gtk_label_new("Listo");
  gtk_widget_set_halign(app->status_label, GTK_ALIGN_START);
  gtk_box_pack_start(GTK_BOX(progress_box), app->status_label, FALSE, FALSE, 0);

  app->detail_label = gtk_label_new("Selecciona las tarjetas y reserva el puerto antes de empezar.");
  gtk_widget_set_halign(app->detail_label, GTK_ALIGN_START);
  gtk_label_set_line_wrap(GTK_LABEL(app->detail_label), TRUE);
  gtk_box_pack_start(GTK_BOX(progress_box), app->detail_label, FALSE, FALSE, 0);

  app->progress_bar = gtk_progress_bar_new();
  gtk_progress_bar_set_show_text(GTK_PROGRESS_BAR(app->progress_bar), TRUE);
  gtk_box_pack_start(GTK_BOX(progress_box), app->progress_bar, FALSE, FALSE, 0);

  app->start_button = gtk_button_new_with_label("Empezar");
  gtk_widget_set_halign(app->start_button, GTK_ALIGN_END);
  g_signal_connect(app->start_button, "clicked", G_CALLBACK(on_start_clicked), app);
  GtkWidget *action_row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 12);
  gtk_widget_set_halign(action_row, GTK_ALIGN_END);
  app->eject_button = gtk_button_new_with_label("Expulsar tarjeta");
  g_signal_connect(app->eject_button, "clicked", G_CALLBACK(on_eject_clicked), app);
  gtk_box_pack_start(GTK_BOX(action_row), app->eject_button, FALSE, FALSE, 0);
  gtk_box_pack_start(GTK_BOX(action_row), app->start_button, FALSE, FALSE, 0);
  gtk_box_pack_start(GTK_BOX(progress_box), action_row, FALSE, FALSE, 0);

  GtkWidget *log_frame = gtk_frame_new("Registro");
  gtk_box_pack_start(GTK_BOX(root), log_frame, TRUE, TRUE, 0);
  GtkWidget *log_scroll = gtk_scrolled_window_new(NULL, NULL);
  gtk_widget_set_vexpand(log_scroll, TRUE);
  gtk_container_add(GTK_CONTAINER(log_frame), log_scroll);
  app->log_view = gtk_text_view_new();
  gtk_text_view_set_editable(GTK_TEXT_VIEW(app->log_view), FALSE);
  gtk_text_view_set_wrap_mode(GTK_TEXT_VIEW(app->log_view), GTK_WRAP_WORD_CHAR);
  app->log_buffer = gtk_text_view_get_buffer(GTK_TEXT_VIEW(app->log_view));
  gtk_container_add(GTK_CONTAINER(log_scroll), app->log_view);

  populate_device_combos(app);
  app_log(app, "Aplicacion iniciada. El helper privilegiado se buscara en: %s", app->helper_path);
  if (!g_file_test(app->helper_path, G_FILE_TEST_EXISTS)) {
    app_log(app, "Aviso: no se encontro el helper. Coloca odroid_sd_helper.sh junto al binario.");
  }

  gtk_widget_show_all(app->window);
  update_ui_state(app);
  gtk_main();

  if (app->devices != NULL) {
    g_ptr_array_free(app->devices, TRUE);
  }
  g_free(app->helper_path);
  g_free(app);
  return 0;
}
