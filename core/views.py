import csv
import uuid

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMessage
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from core.forms import ContactForm, DatasetSearchForm
from core.models import Dataset, Table
from core.templatetags.utils import obfuscate
from core.util import cached_http_get_json
from traffic_control.logging import log_blocked_request


class Echo:
    def write(self, value):
        return value


def contact(request):
    sent = request.GET.get("sent", "").lower() == "true"

    if request.method == "GET":
        data = {}
        if request.user and not request.user.is_anonymous:
            data["name"] = request.user.first_name or request.user.username
            data["email"] = request.user.email
        form = ContactForm(data=data)

    elif request.method == "POST":
        form = ContactForm(data=request.POST)

        if form.is_valid():
            data = form.cleaned_data
            email = EmailMessage(
                subject=f"Contato no Brasil.IO: {data['name']}",
                body=data["message"],
                from_email=f'{data["name"]} (via Brasil.IO) <{settings.DEFAULT_FROM_EMAIL}>',
                to=[settings.DEFAULT_FROM_EMAIL],
                reply_to=[f'{data["name"]} <{data["email"]}>'],
            )
            email.send()
            return redirect(reverse("core:contact") + "?sent=true")

    else:
        context = {"message": "Invalid HTTP method.", "title_4xx": "Oops! Ocorreu um erro:"}
        return render(request, "4xx.html", context, status=405)

    return render(request, "core/contact.html", {"form": form, "sent": sent})


def queryset_to_csv(data, fields):
    header = None
    for row in data.iterator():
        row_data = {}
        for field in fields:
            if not field.show_on_frontend or field.name == "search_data":
                continue
            else:
                value = getattr(row, field.name)
                if field.obfuscate:
                    value = obfuscate(value)
                row_data[field.name] = value
        if header is None:
            header = list(row_data.keys())
            yield header
        yield [row_data[field] for field in header]


def index(request):
    return redirect(reverse("core:home"))


def donate(request):
    return redirect("https://apoia.se/brasilio")


def home(request):
    context = {"datasets": Dataset.objects.filter(show=True).order_by("?")[:6]}
    return render(request, "core/home.html", context)


def dataset_list(request):
    form = DatasetSearchForm(request.GET)
    q = Q(show=True)
    if form.is_valid():
        search_str = form.cleaned_data["search"]
        for term in search_str.split(" "):
            q &= Q(Q(description__icontains=term) | Q(name__icontains=term))
    context = {"datasets": Dataset.objects.filter(q).order_by("name"), "form": form}
    return render(request, "core/dataset-list.html", context)


def dataset_detail(request, slug, tablename=""):
    try:
        dataset = Dataset.objects.get(slug=slug)
    except Dataset.DoesNotExist:
        context = {"message": "Dataset does not exist"}
        return render(request, "404.html", context, status=404)

    if not tablename:
        tablename = dataset.get_default_table().name
        return redirect(reverse("core:dataset-table-detail", kwargs={"slug": slug, "tablename": tablename},))

    try:
        allow_hidden = request.user.is_superuser
        table = dataset.get_table(tablename, allow_hidden=allow_hidden)
    except Table.DoesNotExist:
        context = {"message": "Table does not exist"}
        try:
            # log 404 request only if hidden table exist
            hidden_table = dataset.get_table(tablename, allow_hidden=True)
            if hidden_table:
                log_blocked_request(request, 404)
        except Table.DoesNotExist:
            pass
        return render(request, "404.html", context, status=404)

    querystring = request.GET.copy()
    page_number = querystring.pop("page", ["1"])[0].strip() or "1"
    items_per_page = querystring.pop("items", [str(settings.ROWS_PER_PAGE)])[0].strip() or str(settings.ROWS_PER_PAGE)
    download_csv = querystring.pop("format", [""]) == ["csv"]
    try:
        page = int(page_number)
    except ValueError:
        context = {"message": "Invalid page number."}
        return render(request, "404.html", context, status=404)
    try:
        items_per_page = int(items_per_page)
    except ValueError:
        context = {"message": "Invalid items per page."}
        return render(request, "404.html", context, status=404)
    items_per_page = min(items_per_page, 1000)

    version = dataset.version_set.order_by("-order").first()
    fields = table.fields

    TableModel = table.get_model()
    query, search_query, order_by = TableModel.objects.parse_querystring(querystring)
    all_data = TableModel.objects.composed_query(query, search_query, order_by)

    if download_csv:
        user_agent = request.headers.get("User-Agent", "")
        block_agent = any(True for agent in settings.BLOCKED_AGENTS if agent.lower() in user_agent.lower())

        if not any([query, search_query]) or not user_agent or block_agent:
            # User trying to download a CSV without custom filters or invalid
            # user-agent specified.
            context = {
                "html_code_snippet": "core/400-csv-without-filters.html",
                "download_url": dataset.files_url,
            }
            return render(request, "4xx.html", context, status=400)

        if all_data.count() > settings.CSV_EXPORT_MAX_ROWS:
            context = {"message": "Max rows exceeded.", "title_4xx": "Oops! Ocorreu um erro:"}
            return render(request, "4xx.html", context, status=400)

        filename = "{}-{}.csv".format(slug, uuid.uuid4().hex)
        pseudo_buffer = Echo()
        writer = csv.writer(pseudo_buffer, dialect=csv.excel)
        csv_rows = queryset_to_csv(all_data, fields)
        response = StreamingHttpResponse(
            (writer.writerow(row) for row in csv_rows), content_type="text/csv;charset=UTF-8",
        )
        response["Content-Disposition"] = 'attachment; filename="{}"'.format(filename)
        response.encoding = "UTF-8"
        return response

    paginator = Paginator(all_data, items_per_page)
    data = paginator.get_page(page)

    for key, value in list(querystring.items()):
        if not value:
            del querystring[key]

    context = {
        "data": data,
        "dataset": dataset,
        "fields": fields,
        "max_export_rows": settings.CSV_EXPORT_MAX_ROWS,
        "query_dict": querystring,
        "querystring": querystring.urlencode(),
        "slug": slug,
        "table": table,
        "table": table,
        "total_count": all_data.count(),
        "version": version,
    }
    return render(request, "core/dataset-detail.html", context)


def dataset_suggestion(request):
    return render(request, "core/dataset-suggestion.html", {})


def manifesto(request):
    return render(request, "core/manifesto.html", {})


def collaborate(request):
    return render(request, "core/collaborate.html", {})


def contributors(request):
    url = "https://data.brasil.io/meta/contribuidores.json"
    data = cached_http_get_json(url, 5)
    return render(request, "core/contributors.html", {"contributors": data})


def dataset_files_detail(request, slug):
    dataset = get_object_or_404(Dataset, slug=slug)
    try:
        all_files = dataset.all_files
    except ObjectDoesNotExist:
        return redirect(dataset.get_last_version().download_url)

    if not all_files:
        context = {
            "message": f"<p>Ainda não cadastramos nenhum arquivo para download no dataset {slug}.</p><p>Estamos trabalhando para os dados estarem disponíveis em breve.</p><p>Acompanhe o nosso <a href='https://t.me/brasil_io'>grupo no Telegram</a> para manter-se atualizada.</p>",
        }
        return render(request, "404.html", context)

    context = {
        "dataset": dataset,
        "capture_date": max([t.collect_date for t in dataset.tables]),
        "file_list": all_files,
    }
    return render(request, "core/dataset_files_list.html", context)
