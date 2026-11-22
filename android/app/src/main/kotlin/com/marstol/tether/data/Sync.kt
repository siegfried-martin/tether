package com.marstol.tether.data

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import androidx.datastore.preferences.core.Preferences
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.state.getAppWidgetState
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.workDataOf
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.marstol.tether.widget.ACCOUNT
import com.marstol.tether.widget.AgendaWidget
import com.marstol.tether.widget.Archiving
import com.marstol.tether.widget.Pending
import com.marstol.tether.widget.InboxWidget
import com.marstol.tether.widget.DATA_VERSION
import com.marstol.tether.widget.DayWidget
import com.marstol.tether.widget.TAG_ID
import com.marstol.tether.widget.TasksWidget
import com.marstol.tether.widget.WidgetData
import java.io.IOException
import java.time.LocalDate
import java.time.ZoneId
import java.util.concurrent.TimeUnit

/** Fetches widget data in the background and redraws the widgets. */
object Sync {
    private const val TAG = "TetherSync"
    /** The calendar cache runs from yesterday, so the Day widget can step back one day. */
    const val CALENDAR_DAYS = 15

    private val network = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    /** Every 15 minutes (Android's minimum); unchanged data costs a bodiless 304. */
    fun schedule(context: Context) {
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            "tether-sync", ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES).setConstraints(network).build())
    }

    /**
     * Sync as soon as there is a network. A tap on refresh syncs at once even offline,
     * so the widgets can say they are offline.
     */
    fun now(context: Context, waitForNetwork: Boolean = true) {
        val work = OneTimeWorkRequestBuilder<SyncWorker>()
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
        if (waitForNetwork) work.setConstraints(network)
        WorkManager.getInstance(context).enqueueUniqueWork("tether-sync-now", ExistingWorkPolicy.REPLACE, work.build())
    }

    /**
     * A widget button's server call (complete a task, archive an email), then a sync. Run as
     * background work: a widget tap must return at once, and Android may freeze or kill the
     * app as soon as it does.
     */
    fun act(context: Context, kind: String, id: Int) {
        WorkManager.getInstance(context).enqueue(
            OneTimeWorkRequestBuilder<ActionWorker>()
                .setInputData(workDataOf(ActionWorker.KIND to kind, ActionWorker.ID to id))
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .setConstraints(network)
                .build())
    }

    suspend fun run(context: Context) {
        try {
            if (!Tokens.signedIn(context)) throw Api.SignedOut()
            // Days follow the phone's zone, so the widgets travel with the owner.
            val tz = Uri.encode(ZoneId.systemDefault().id)
            val start = LocalDate.now().minusDays(1)
            fetch(context, "/api/widget/calendar?start=$start&days=$CALENDAR_DAYS&tz=$tz", Store.CALENDAR)
            for (tag in taskTags(context)) {
                fetch(context, "/api/widget/tasks?limit=50&tz=$tz" + (tag?.let { "&tag=$it" } ?: ""), Store.tasksName(tag))
            }
            for (account in inboxAccounts(context)) {
                fetch(context, "/api/widget/inbox" + (account?.let { "?account=" + Uri.encode(it) } ?: ""),
                      Store.inboxName(account))
            }
            Store.setStatus(context, null)
        } catch (e: Api.SignedOut) {
            Store.setStatus(context, "signed_out")
        } catch (e: IOException) {
            Log.w(TAG, "sync failed: ${e.javaClass.simpleName}")
            Store.setStatus(context, "offline")
        }
        redraw(context)
    }

    private suspend fun fetch(context: Context, path: String, name: String) {
        try {
            val resp = Api.get(context, path, Store.etag(context, name))
            if (resp.body != null) Store.write(context, name, resp.body, resp.etag)
        } catch (e: IOException) {
            // A tag deleted since its widget was set up answers 404: drop that cache so the widget says so.
            if (e.message == "HTTP 404") Store.write(context, name, "{\"missing\":true}", null) else throw e
        }
    }

    /** Every tag filter in use by a Tasks widget, plus null for "all tasks". */
    private suspend fun taskTags(context: Context): Set<Int?> {
        val manager = GlanceAppWidgetManager(context)
        val tags = mutableSetOf<Int?>(null)
        for (id in manager.getGlanceIds(TasksWidget::class.java)) {
            val prefs: Preferences = getAppWidgetState(context, PreferencesGlanceStateDefinition, id)
            prefs[TAG_ID]?.let { tags.add(it) }
        }
        return tags
    }

    /** Every account filter in use by an Inbox widget (null for all), or none without an Inbox widget. */
    private suspend fun inboxAccounts(context: Context): Set<String?> {
        val ids = GlanceAppWidgetManager(context).getGlanceIds(InboxWidget::class.java)
        return ids.map { getAppWidgetState(context, PreferencesGlanceStateDefinition, it)[ACCOUNT] }.toSet()
    }

    suspend fun redraw(context: Context) {
        WidgetData.version.value += 1
        val manager = GlanceAppWidgetManager(context)
        for (widget in listOf(TasksWidget(), InboxWidget(), AgendaWidget(), DayWidget())) {
            val ids = manager.getGlanceIds(widget.javaClass)
            Log.d(TAG, "redraw v=${WidgetData.version.value} ${widget.javaClass.simpleName} x${ids.size}")
            for (id in ids) {
                updateAppWidgetState(context, id) { it[DATA_VERSION] = (it[DATA_VERSION] ?: 0) + 1 }
                widget.update(context, id)
            }
        }
    }
}

/** A new time zone (landing somewhere) or a clock change: redo the days at once. */
class ClockChangeReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        Sync.now(context)
    }
}

class ActionWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val id = inputData.getInt(ID, -1)
        val kind = inputData.getString(KIND)
        val (path, pending) = when (kind) {
            COMPLETE -> "/api/tasks/$id/complete" to Pending
            ARCHIVE -> "/api/items/$id/archive" to Archiving
            else -> return Result.failure()
        }
        try {
            Api.post(applicationContext, path)
        } catch (e: Api.SignedOut) {
            // shown by the widgets
        } catch (e: IOException) {
            // offline or refused: the row shows again
            Log.w("TetherSync", "$kind failed: ${e.javaClass.simpleName}")
        }
        Sync.run(applicationContext)
        pending.remove(applicationContext, id)
        Sync.redraw(applicationContext)
        return Result.success()
    }

    companion object {
        const val KIND = "kind"
        const val ID = "id"
        const val COMPLETE = "complete"
        const val ARCHIVE = "archive"
    }
}

class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        Sync.run(applicationContext)
        return Result.success()
    }
}
