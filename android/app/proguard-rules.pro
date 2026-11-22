# Glance creates widget action callbacks by class name.
-keep class * implements androidx.glance.appwidget.action.ActionCallback { <init>(); }
# WorkManager creates workers by class name.
-keep class * extends androidx.work.ListenableWorker { <init>(...); }
