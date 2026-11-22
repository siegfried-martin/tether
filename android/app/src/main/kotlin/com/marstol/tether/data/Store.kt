package com.marstol.tether.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.LocalDate

/**
 * The last widget data fetched from the server, kept in the app's private files
 * so widgets draw at once (and offline) and polls can send the ETag.
 */
object Store {
    private fun dir(context: Context) = File(context.filesDir, "widget").apply { mkdirs() }

    fun read(context: Context, name: String): String? =
        File(dir(context), "$name.json").takeIf { it.exists() }?.readText()

    fun etag(context: Context, name: String): String? =
        File(dir(context), "$name.etag").takeIf { it.exists() }?.readText()

    fun write(context: Context, name: String, body: String, etag: String?) {
        File(dir(context), "$name.json").writeText(body)
        val tagFile = File(dir(context), "$name.etag")
        if (etag != null) tagFile.writeText(etag) else tagFile.delete()
    }

    fun clear(context: Context) {
        dir(context).deleteRecursively()
    }

    /** Status of the last sync, shown on the widgets when something is wrong. */
    fun setStatus(context: Context, error: String?) {
        context.getSharedPreferences("sync", Context.MODE_PRIVATE).edit()
            .putString("error", error).putLong("at", System.currentTimeMillis()).apply()
    }

    fun status(context: Context): String? =
        context.getSharedPreferences("sync", Context.MODE_PRIVATE).getString("error", null)

    fun tasksName(tagId: Int?) = if (tagId == null) "tasks" else "tasks-$tagId"
    const val CALENDAR = "calendar"
}

// ------------------------------------------------------------------ models (see src/tether/services/widget.py)

data class Tag(val id: Int, val name: String, val color: String)

data class TaskItem(
    val id: Int,
    val title: String,
    val due: String?,          // overdue, today, tomorrow, later, or null
    val dueAt: String?,
    val priority: String?,
    val tags: List<Pair<String, String>>,   // name to color
    val url: String,
    val subtasks: List<TaskItem>,
)

data class TaskList(val today: String, val count: Int, val tasks: List<TaskItem>, val tag: Tag?, val tags: List<Tag>)

data class Event(
    val id: Int,
    val title: String,
    val start: String,
    val end: String?,
    val allDay: Boolean,
    val location: String?,
    val busy: String?,
    val color: String?,
    val continued: Boolean,
    val continues: Boolean,
    val url: String,
)

data class Day(val date: LocalDate, val events: List<Event>)

data class Calendar(val today: LocalDate, val days: List<Day>)

private fun JSONObject.str(key: String): String? = if (isNull(key)) null else optString(key)

private fun <T> JSONArray.map(f: (JSONObject) -> T): List<T> = (0 until length()).map { f(getJSONObject(it)) }

private fun tag(o: JSONObject) = Tag(o.getInt("id"), o.getString("name"), o.getString("color"))

private fun task(o: JSONObject): TaskItem = TaskItem(
    id = o.getInt("id"),
    title = o.getString("title"),
    due = o.str("due"),
    dueAt = o.str("due_at"),
    priority = o.str("priority"),
    tags = o.getJSONArray("tags").map { it.getString("name") to it.getString("color") },
    url = o.getString("url"),
    subtasks = o.optJSONArray("subtasks")?.map(::task) ?: emptyList(),
)

fun parseTasks(body: String): TaskList {
    val o = JSONObject(body)
    return TaskList(
        today = o.getString("today"),
        count = o.getInt("count"),
        tasks = o.getJSONArray("tasks").map(::task),
        tag = o.optJSONObject("tag")?.let(::tag),
        tags = o.getJSONArray("tags").map(::tag),
    )
}

fun parseCalendar(body: String): Calendar {
    val o = JSONObject(body)
    return Calendar(
        today = LocalDate.parse(o.getString("today")),
        days = o.getJSONArray("days").map { d ->
            Day(LocalDate.parse(d.getString("date")), d.getJSONArray("events").map { e ->
                Event(
                    id = e.getInt("id"),
                    title = e.getString("title"),
                    start = e.getString("start"),
                    end = e.str("end"),
                    allDay = e.getBoolean("all_day"),
                    location = e.str("location"),
                    busy = e.str("busy"),
                    color = e.str("color"),
                    continued = e.getBoolean("continued"),
                    continues = e.getBoolean("continues"),
                    url = e.getString("url"),
                )
            })
        },
    )
}
