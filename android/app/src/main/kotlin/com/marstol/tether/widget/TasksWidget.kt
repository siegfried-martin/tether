package com.marstol.tether.widget

import android.content.Context
import android.content.Intent
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import androidx.datastore.preferences.core.Preferences
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.LocalContext
import androidx.glance.action.ActionParameters
import androidx.glance.action.actionParametersOf
import androidx.glance.action.clickable
import androidx.glance.appwidget.CheckBox
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.action.actionStartActivity
import androidx.glance.appwidget.lazy.LazyColumn
import androidx.glance.appwidget.lazy.items
import androidx.glance.appwidget.provideContent
import androidx.glance.currentState
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.padding
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import com.marstol.tether.QuickAddActivity
import com.marstol.tether.R
import com.marstol.tether.data.Api
import com.marstol.tether.data.Store
import com.marstol.tether.data.Sync
import com.marstol.tether.data.TaskItem
import com.marstol.tether.data.parseTasks
import java.io.IOException
import java.time.LocalDate
import java.time.format.DateTimeFormatter

/** Open tasks in the owner's order, optionally one tag's, with a checkbox to complete each. */
class TasksWidget : GlanceAppWidget() {
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        provideContent {
            val tagId = currentState<Preferences>()[TAG_ID]
            Content(tagId)
        }
    }

    @Composable
    private fun Content(tagId: Int?) {
        val context = LocalContext.current
        val body = Store.read(context, Store.tasksName(tagId))
        val list = body?.takeUnless { it.contains("\"missing\"") }?.let { runCatching { parseTasks(it) }.getOrNull() }
        val done = Pending.ids(context)
        val title = list?.tag?.name ?: "Tasks"
        val page = if (tagId != null) "/tasks?tag=$tagId" else "/tasks"
        val add = actionStartActivity(Intent(context, QuickAddActivity::class.java)
            .putExtra(QuickAddActivity.EXTRA_TAG, list?.tag?.name)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_MULTIPLE_TASK))
        WidgetFrame {
            Header(if (list != null) "$title  ${list.count}" else title, openInApp(context, page),
                   R.drawable.ic_add to add, refreshIcon to actionRunCallback<RefreshAction>())
            when {
                body != null && list == null -> Placeholder(context, "That tag is gone. Long-press the widget to pick another.")
                list == null -> Placeholder(context, "Loading tasks…")
                else -> {
                    val rows = list.tasks.filter { it.id !in done }.flatMap { t ->
                        listOf(t to false) + t.subtasks.filter { it.id !in done }.map { it to true }
                    }
                    if (rows.isEmpty()) Placeholder(context, "No open tasks")
                    else LazyColumn(GlanceModifier.defaultWeight()) {
                        items(rows, itemId = { it.first.id.toLong() }) { (task, sub) ->
                            TaskRow(task, sub, LocalDate.parse(list.today))
                        }
                    }
                    SyncNote()
                }
            }
        }
    }

    @Composable
    private fun TaskRow(task: TaskItem, sub: Boolean, today: LocalDate) {
        val context = LocalContext.current
        Row(GlanceModifier.fillMaxWidth().padding(start = if (sub) 24.dp else 0.dp, bottom = 2.dp),
            verticalAlignment = Alignment.CenterVertically) {
            CheckBox(checked = false, onCheckedChange = actionRunCallback<CompleteTask>(
                actionParametersOf(CompleteTask.ID to task.id)))
            Column(GlanceModifier.defaultWeight().clickable(openInApp(context, task.url))) {
                Text(task.title, maxLines = 2,
                     style = TextStyle(fontSize = Body, color = GlanceTheme.colors.onSurface))
                val due = dueLabel(task, today)
                val tags = task.tags.joinToString(" · ") { it.first }
                val meta = listOfNotNull(due, tags.ifEmpty { null }).joinToString("  ·  ")
                if (meta.isNotEmpty()) {
                    Text(meta, maxLines = 1, style = TextStyle(fontSize = Muted,
                         color = if (task.due == "overdue") GlanceTheme.colors.error
                                 else if (task.due == "today") GlanceTheme.colors.primary
                                 else GlanceTheme.colors.onSurfaceVariant))
                }
            }
        }
    }

    private fun dueLabel(task: TaskItem, today: LocalDate): String? {
        val at = task.dueAt?.take(10)?.let { runCatching { LocalDate.parse(it) }.getOrNull() } ?: return null
        val date = at.format(DateTimeFormatter.ofPattern(if (at.year == today.year) "MMM d" else "MMM d, yyyy"))
        return when (task.due) {
            "overdue" -> "Overdue · $date"
            "today" -> "Today"
            "tomorrow" -> "Tomorrow"
            else -> date
        }
    }
}

/** Tasks checked off in the widget, hidden at once while the server catches up. */
object Pending {
    private fun prefs(context: Context) = context.getSharedPreferences("pending", Context.MODE_PRIVATE)
    fun ids(context: Context): Set<Int> =
        prefs(context).getStringSet("done", emptySet())!!.mapNotNull { it.toIntOrNull() }.toSet()
    fun add(context: Context, id: Int) = prefs(context).edit().putStringSet("done", ids(context).map { "$it" }.toSet() + "$id").apply()
    fun remove(context: Context, id: Int) = prefs(context).edit().putStringSet("done", (ids(context) - id).map { "$it" }.toSet()).apply()
}

class CompleteTask : ActionCallback {
    override suspend fun onAction(context: Context, glanceId: GlanceId, parameters: ActionParameters) {
        val id = parameters[ID] ?: return
        Pending.add(context, id)
        Sync.redraw(context)
        try {
            Api.post(context, "/api/tasks/$id/complete")
        } catch (e: Api.SignedOut) {
            // shown by the widget
        } catch (e: IOException) {
            // offline or refused: the task shows again
        }
        Sync.run(context)
        Pending.remove(context, id)
        Sync.redraw(context)
    }

    companion object {
        val ID = ActionParameters.Key<Int>("task_id")
    }
}

class TasksWidgetReceiver : TetherReceiver() {
    override val glanceAppWidget = TasksWidget()
}
