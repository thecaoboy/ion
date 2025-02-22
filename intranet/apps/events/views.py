import logging
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from django import http
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import exceptions
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ...utils.helpers import get_id
from ...utils.html import safe_html
from ..auth.decorators import deny_restricted
from .forms import AdminEventForm, EventForm
from .models import Event

logger = logging.getLogger(__name__)


def date_format(date):
    try:
        d = date.strftime("%Y-%m-%d")
    except ValueError:
        return None
    return d


def decode_date(date):
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None
    return d


def is_weekday(date):
    return date.isoweekday() in range(1, 6)


def events_context(request, date=None):
    local_time = timezone.localtime()

    if date is None:
        date = local_time

    date_today = date.replace(hour=0, minute=0, second=0, microsecond=0)

    if timezone.is_naive(date_today):
        date_today = timezone.make_aware(date_today)

    date_tomorrow = date_today + timedelta(days=1)
    viewable_events = Event.objects.visible_to_user(request.user).prefetch_related("groups")
    events = viewable_events.filter(time__range=[date_today, date_tomorrow])

    display = True
    has_events = True

    if not events:
        has_events = False

    if not events and not is_weekday(date):  # don't display if there are no events and it is a weekend
        display = False

    data = {
        "events_ctx": {
            "date": date,
            "date_today": date_format(date_today),
            "events": events,
            "display": display,
            "has_events": has_events,
        }
    }
    return data


def week_data(request, date=None):
    if date:
        start_date = date
    elif "date" in request.GET:
        start_date = decode_date(request.GET["date"])
    else:
        start_date = events_context(request)["events_ctx"]["date"]

    delta = start_date.isoweekday() - 1
    start_date -= timedelta(days=delta)

    days = []
    for i in range(7):
        new_date = start_date + timedelta(days=i)
        days.append(events_context(request, date=new_date))

    next_week = date_format(start_date + timedelta(days=7))
    last_week = date_format(start_date - timedelta(days=7))
    today = date_format(timezone.localtime())

    data = {
        "days": days,
        "next_week": next_week,
        "last_week": last_week,
        "today": today,
    }
    return data


def month_data(request):
    if "date" in request.GET:
        start_date = decode_date(request.GET["date"])
    else:
        start_date = timezone.localtime()

    delta = (int(date_format(start_date)[8:]) // 7) * 7
    start_date -= timedelta(days=delta)

    week1 = week_data(request, start_date)
    week2 = week_data(request, date=decode_date(week1["next_week"]))
    week3 = week_data(request, date=decode_date(week2["next_week"]))
    week4 = week_data(request, date=decode_date(week3["next_week"]))
    week5 = week_data(request, date=decode_date(week4["next_week"]))

    month = start_date.strftime("%B")
    one_month = relativedelta(months=1)
    next_month = date_format(start_date + one_month)
    last_month = date_format(start_date - one_month)

    data = {"weeks": [week1, week2, week3, week4, week5], "next_month": next_month, "last_month": last_month, "current_month": month}
    return data


@login_required
@deny_restricted
def events_view(request):
    """Events homepage.

    Shows a list of events occurring in the next week, month, and
    future.
    """

    is_events_admin = request.user.has_admin_permission("events")

    if request.method == "POST":
        if "approve" in request.POST and is_events_admin:
            event_id = get_id(request.POST.get("approve"))
            if event_id:
                event = get_object_or_404(Event, id=event_id)
                event.rejected = False
                event.approved = True
                event.approved_by = request.user
                event.save()
                messages.success(request, "Approved event {}".format(event))
                logger.info("Admin %s approved event: %s (%s)", request.user, event, event.id)
            else:
                raise http.Http404

        if "reject" in request.POST and is_events_admin:
            event_id = get_id(request.POST.get("reject"))
            if event_id:
                event = get_object_or_404(Event, id=event_id)
                event.approved = False
                event.rejected = True
                event.rejected_by = request.user
                event.save()
                messages.success(request, "Rejected event {}".format(event))
                logger.info("Admin %s rejected event: %s (%s)", request.user, event, event.id)
            else:
                raise http.Http404

    show_all = False
    if is_events_admin:
        viewable_events = Event.objects.all().filter(rejected=False).this_year().prefetch_related("groups")
        if "show_all" in request.GET:
            show_all = True
    else:
        viewable_events = Event.objects.visible_to_user(request.user).this_year().prefetch_related("groups")

    classic = False  # show classic view
    if "classic" in request.GET:
        classic = True

    # get date objects for week and month
    today = timezone.localtime()
    delta = today - timezone.timedelta(days=today.weekday())
    this_week = (delta, delta + timezone.timedelta(days=7))
    this_month = (this_week[1], this_week[1] + timezone.timedelta(days=31))

    def has_special_event(events):
        for event in events:
            if event.show_attending or event.scheduled_activity or event.announcement or event.show_on_dashboard:
                return True
        return False

    events_categories = [
        {"title": "This week", "events": viewable_events.filter(time__gte=this_week[0], time__lt=this_week[1], approved=True)},
        {"title": "This month", "events": viewable_events.filter(time__gte=this_month[0], time__lt=this_month[1], approved=True)},
    ]

    if not show_all and not classic:
        events_categories.append(
            {
                "title": "Week and month",
                "events": viewable_events.filter(time__gte=this_week[0], time__lt=this_month[1], approved=True),
                "has_special_event": has_special_event(viewable_events.filter(time__gte=this_week[0], time__lt=this_month[1])),
            }
        )

    if is_events_admin:
        unapproved_events = viewable_events.filter(approved=False).prefetch_related("groups")
        past_events = viewable_events.filter(time__lt=this_week[0], approved=True)
        future_events = viewable_events.filter(time__gte=this_month[1], approved=True)

        events_categories = [{"title": "Awaiting Approval", "events": unapproved_events}] + events_categories

        if show_all:
            events_categories.append({"title": "Future", "events": future_events})
            events_categories.append({"title": "Past", "events": past_events})

    num_events = viewable_events.filter(time__gte=this_week[0], time__lt=this_month[1]).count()

    context = {
        "events": events_categories,
        "num_events": num_events,
        "is_events_admin": is_events_admin,
        "show_attend": True,
        "show_icon": False,
        "show_all": show_all,
        "classic": classic,
    }
    context["week_data"] = week_data(request)
    context["month_data"] = month_data(request)

    if "view" in request.GET and request.GET["view"] == "month":
        context["view"] = "month"
    else:
        context["view"] = "week"

    return render(request, "events/home.html", context)


@login_required
@deny_restricted
def join_event_view(request, event_id):
    """The join event page.

    If a POST request, actually add or remove the attendance of the current
    user. Otherwise, display a page with confirmation.

    Args:
        event_id (int): event id
    """

    event = get_object_or_404(Event, id=event_id)
    is_events_admin = request.user.has_admin_permission("events")

    if not event.approved and not is_events_admin:
        raise http.Http404

    if not event.show_attending:
        return redirect("events")

    if request.method == "POST":
        if not event.approved:
            messages.error(request, "You cannot attend this event because it has not been approved yet.")
            return redirect("events")

        if "attending" in request.POST:
            attending = request.POST.get("attending")

            if attending == "true":
                event.attending.add(request.user)
            else:
                event.attending.remove(request.user)

            return redirect("events")

    context = {"event": event, "is_events_admin": is_events_admin}
    return render(request, "events/join_event.html", context)


@login_required
@deny_restricted
def event_roster_view(request, event_id):
    """Show the event roster.

    Users with hidden eighth period permissions will not be displayed in the public roster.
    Users will be able to view all other viewable users, along with a count of the number of hidden users.
    Event admins will see both the public and full roster.

    Args:
        event_id (int): the Event id
    """

    event = get_object_or_404(Event, id=event_id)
    is_events_admin = request.user.has_admin_permission("events")

    if not event.approved and not is_events_admin:
        raise http.Http404

    full_roster = event.attending.all()
    viewable_roster = []
    num_hidden_members = 0
    for user in full_roster:
        if user.can_view_eighth:
            viewable_roster.append(user)
        else:
            num_hidden_members += 1

    context = {
        "event": event,
        "viewable_roster": viewable_roster,
        "full_roster": full_roster,
        "num_hidden_members": num_hidden_members,
        "is_events_admin": is_events_admin,
    }
    return render(request, "events/roster.html", context)


@login_required
@deny_restricted
def add_event_view(request):
    """Add event page.

    An events administrator can create an event directly without
    approval.
    """

    is_events_admin = request.user.has_admin_permission("events")
    if not is_events_admin:
        return redirect("request_event")

    if request.method == "POST":
        form = EventForm(data=request.POST, all_groups=request.user.has_admin_permission("groups"))
        if form.is_valid():
            obj = form.save()
            obj.user = request.user
            # SAFE HTML
            obj.description = safe_html(obj.description)

            # auto-approve
            obj.approved = True
            obj.approved_by = request.user
            messages.success(request, "Because you are an administrator, this event was auto-approved.")
            logger.info("Admin %s added event: %s (%s)", request.user, obj, obj.id)
            obj.created_hook(request)

            obj.save()
            return redirect("events")
        else:
            messages.error(request, "Error adding event.")
    else:
        form = EventForm(all_groups=request.user.has_admin_permission("groups"))
    context = {"form": form, "action": "add", "action_title": "Add", "is_events_admin": is_events_admin}
    return render(request, "events/add_modify.html", context)


@login_required
@deny_restricted
def request_event_view(request):
    """Request event page.

    The event is added in the system but must be approved by
    an administrator.
    """

    if request.method == "POST":
        form = EventForm(data=request.POST, all_groups=request.user.has_admin_permission("groups"))
        if form.is_valid():
            obj = form.save()
            obj.user = request.user
            # SAFE HTML
            obj.description = safe_html(obj.description)

            messages.success(
                request, "Your event needs to be approved by an administrator. If approved, it should appear on Intranet within 24 hours."
            )
            obj.created_hook(request)

            obj.save()
            return redirect("events")
        else:
            messages.error(request, "Error adding event.")
    else:
        form = EventForm(all_groups=request.user.has_admin_permission("groups"))
    context = {"form": form, "action": "request", "action_title": "Submit", "is_events_admin": False}
    return render(request, "events/add_modify.html", context)


@login_required
@deny_restricted
def modify_event_view(request, event_id):
    """Modify event page. You may only modify an event if you are an
    administrator.

    Args:
        event_id (int): event id
    """

    event = get_object_or_404(Event, id=event_id)
    is_events_admin = request.user.has_admin_permission("events")

    if not is_events_admin:
        raise exceptions.PermissionDenied

    if request.method == "POST":
        form = AdminEventForm(data=request.POST, instance=event, all_groups=request.user.has_admin_permission("groups"))
        if form.is_valid():
            obj = form.save()
            # SAFE HTML
            obj.description = safe_html(obj.description)
            obj.save()
            messages.success(request, "Successfully modified event.")
            logger.info("Admin %s modified event: %s (%s)", request.user, obj, obj.id)
        else:
            messages.error(request, "Error modifying event.")
    else:
        form = AdminEventForm(instance=event, all_groups=request.user.has_admin_permission("groups"))
    context = {"form": form, "action": "modify", "action_title": "Modify", "id": event_id, "is_events_admin": is_events_admin}
    return render(request, "events/add_modify.html", context)


@login_required
@deny_restricted
def delete_event_view(request, event_id):
    """Delete event page. You may only delete an event if you are an
    administrator. Confirmation page if not POST.

    Args:
        event_id: event id
    """
    event = get_object_or_404(Event, id=event_id)
    if not request.user.has_admin_permission("events"):
        raise exceptions.PermissionDenied

    if request.method == "POST":
        event.delete()
        messages.success(request, "Successfully deleted event.")
        logger.info("Admin %s deleted event: %s (%s)", request.user, event, event.id)
        return redirect("events")
    else:
        return render(request, "events/delete.html", {"event": event, "action": "delete"})


@login_required
@deny_restricted
def show_event_view(request):
    """Unhide an event that was hidden by the logged-in user.

    events_hidden in the user model is the related_name for
    "users_hidden" in the EventUserMap model.
    """
    if request.method == "POST":
        event_id = get_id(request.POST.get("event_id", None))
        if event_id:
            event = get_object_or_404(Event, id=event_id)
            event.user_map.users_hidden.remove(request.user)
            event.user_map.save()
            return http.HttpResponse("Unhidden")
        raise http.Http404
    else:
        return http.HttpResponseNotAllowed(["POST"], "HTTP 405: METHOD NOT ALLOWED")


@login_required
@deny_restricted
def hide_event_view(request):
    """Hide an event for the logged-in user.

    events_hidden in the user model is the related_name for
    "users_hidden" in the EventUserMap model.
    """
    if request.method == "POST":
        event_id = get_id(request.POST.get("event_id", None))
        if event_id:
            event = get_object_or_404(Event, id=event_id)
            event.user_map.users_hidden.add(request.user)
            event.user_map.save()
            return http.HttpResponse("Hidden")
        raise http.Http404
    else:
        return http.HttpResponseNotAllowed(["POST"], "HTTP 405: METHOD NOT ALLOWED")
