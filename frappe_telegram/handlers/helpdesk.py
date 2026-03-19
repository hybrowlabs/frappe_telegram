import json
import re

import frappe
from frappe.utils.file_manager import save_file as save_file_to_disk

from frappe_telegram.handlers.telegram_api import (
	answer_callback_query,
	download_telegram_file,
	get_file_info,
	send_message_api,
)


def process_update(update_data, token, settings):
	"""Process a single Telegram update through the helpdesk state machine."""
	frappe.set_user("Administrator")

	# Extract message or callback_query
	message = update_data.get("message")
	callback_query = update_data.get("callback_query")

	if callback_query:
		callback_data = callback_query.get("data", "")
		user_info = callback_query.get("from", {})
		chat_info = callback_query.get("message", {}).get("chat", {})
		text = ""
		# Acknowledge the callback
		answer_callback_query(callback_query["id"], token)
	elif message:
		callback_data = ""
		user_info = message.get("from", {})
		chat_info = message.get("chat", {})
		text = message.get("text", "") or message.get("caption", "")
	else:
		return

	if not user_info.get("id") or not chat_info.get("id"):
		return

	chat_id = chat_info["id"]

	# Get or create Telegram User + Chat
	telegram_user = get_or_create_telegram_user(user_info)
	telegram_chat = get_or_create_telegram_chat(chat_info, telegram_user)

	# Load or create conversation state
	state = get_or_create_conversation_state(telegram_user.name, telegram_chat.name)

	# Route based on command / callback / current state
	if text == "/start":
		reset_conversation(state)
		send_welcome_menu(chat_id, token, settings)

	elif text == "/newticket" or callback_data == "create_ticket":
		handle_new_ticket(telegram_user, telegram_chat, chat_id, token, settings, state)

	elif callback_data == "my_tickets":
		handle_my_tickets(telegram_user, chat_id, token)

	elif text == "/cancel":
		reset_conversation(state)
		send_message_api(chat_id, token, "❌ Ticket creation cancelled. Send /start to see options.")

	elif state.state == "awaiting_email":
		handle_email_input(text or callback_data, telegram_user, chat_id, token, settings, state)

	elif state.state == "collecting_fields":
		# Handle both text input and callback_data (from inline keyboard buttons)
		# Prefer text input, fallback to callback_data for inline keyboard selections
		input_value = text if text and text.strip() else (callback_data if callback_data else "")
		if not input_value:
			send_message_api(chat_id, token, "⚠️ Please provide a response.")
			return
		handle_field_input(input_value, telegram_user, telegram_chat, chat_id, token, settings, state)

	elif callback_data == "submit_ticket":
		handle_submit_ticket(telegram_user, telegram_chat, chat_id, token, settings, state)

	elif callback_data == "cancel_ticket":
		reset_conversation(state)
		send_message_api(chat_id, token, "❌ Ticket creation cancelled. Send /start to see options.")

	elif callback_data.startswith("reopen_ticket_"):
		handle_reopen_ticket(callback_data, telegram_user, chat_id, token)

	elif callback_data == "edit_ticket":
		show_edit_field_menu(state, chat_id, token)

	elif callback_data == "attach_document":
		handle_attach_document_start(state, chat_id, token)

	elif callback_data == "skip_to_review" or callback_data == "done_attaching":
		show_ticket_review(state, telegram_user, telegram_chat, chat_id, token, settings)

	elif callback_data.startswith("edit_field_"):
		field_key = callback_data.replace("edit_field_", "")
		handle_edit_field(field_key, telegram_user, telegram_chat, chat_id, token, settings, state)

	elif state.state == "awaiting_attachment":
		handle_attachment_upload(message, state, chat_id, token)

	elif state.state == "reviewing_ticket":
		# Handle any text input during review (shouldn't happen, but handle gracefully)
		show_ticket_review(state, telegram_user, telegram_chat, chat_id, token, settings)

	elif state.state == "editing_field":
		handle_editing_field_input(text or callback_data, telegram_user, telegram_chat, chat_id, token, settings, state)

	else:
		# Not in a conversation — check for follow-up to open ticket
		handle_followup_or_prompt(text, telegram_user, telegram_chat, chat_id, token, message)


# --- User / Chat management ---

def get_or_create_telegram_user(user_info):
	"""Get or create a Telegram User record from Telegram API user data."""
	user_id = str(user_info["id"])
	existing = frappe.db.get_value("Telegram User", {"telegram_user_id": user_id})
	if existing:
		return frappe.get_doc("Telegram User", existing)

	full_name = user_info.get("first_name", "")
	if user_info.get("last_name"):
		full_name += " " + user_info["last_name"]

	doc = frappe.get_doc({
		"doctype": "Telegram User",
		"telegram_user_id": user_id,
		"telegram_username": user_info.get("username", ""),
		"full_name": full_name.strip() or "Unknown",
		"is_guest": 1,
	})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def get_or_create_telegram_chat(chat_info, telegram_user=None):
	"""Get or create a Telegram Chat record."""
	chat_id = str(chat_info["id"])
	existing = frappe.db.get_value("Telegram Chat", {"chat_id": chat_id})
	if existing:
		return frappe.get_doc("Telegram Chat", existing)

	title = (
		chat_info.get("title")
		or chat_info.get("username")
		or chat_info.get("first_name")
		or str(chat_id)
	)
	doc = frappe.get_doc({
		"doctype": "Telegram Chat",
		"chat_id": chat_id,
		"title": title,
		"type": chat_info.get("type", "private"),
	})
	if telegram_user:
		doc.append("users", {"telegram_user": telegram_user.name})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


# --- Conversation state management ---

def get_or_create_conversation_state(telegram_user_name, telegram_chat_name):
	"""Get or create a conversation state for a Telegram user."""
	existing = frappe.db.get_value(
		"Telegram Conversation State",
		{"telegram_user": telegram_user_name},
	)
	if existing:
		return frappe.get_doc("Telegram Conversation State", existing)

	doc = frappe.get_doc({
		"doctype": "Telegram Conversation State",
		"telegram_user": telegram_user_name,
		"telegram_chat": telegram_chat_name,
		"state": "idle",
		"collected_data": "{}",
		"current_field_index": 0,
	})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def reset_conversation(state):
	"""Reset conversation state to idle and clean up orphaned attachments."""
	# Delete any unattached files from a cancelled ticket creation
	try:
		data = json.loads(state.collected_data or "{}")
		for file_name in data.get("_attachments", []):
			if frappe.db.exists("File", file_name):
				file_doc = frappe.get_doc("File", file_name)
				if not file_doc.attached_to_doctype:
					file_doc.delete(ignore_permissions=True)
	except Exception:
		pass

	state.state = "idle"
	state.collected_data = "{}"
	state.current_field_index = 0
	state.save(ignore_permissions=True)


# --- Welcome menu ---

def send_welcome_menu(chat_id, token, settings):
	"""Send welcome message with inline keyboard buttons."""
	welcome = settings.welcome_message or "Welcome to Support! How can I help you?"
	keyboard = {
		"inline_keyboard": [
			[{"text": "🎫 Create Ticket", "callback_data": "create_ticket"}],
			[{"text": "📋 My Tickets", "callback_data": "my_tickets"}],
		]
	}
	send_message_api(chat_id, token, welcome, reply_markup=keyboard)


# --- New ticket flow ---

def handle_new_ticket(telegram_user, telegram_chat, chat_id, token, settings, state):
	"""Start the new ticket creation flow."""
	# Check if email is already stored
	email = state.email
	if email:
		# Skip email collection, start field collection
		init_field_collection(state, settings)
		ask_next_field(state, chat_id, token)
	else:
		# Ask for email
		state.state = "awaiting_email"
		state.telegram_chat = telegram_chat.name
		state.save(ignore_permissions=True)
		send_message_api(chat_id, token, "📧 Please share your registered email to continue.")


def handle_email_input(text, telegram_user, chat_id, token, settings, state):
	"""Validate and store the user's email."""
	if not text or not re.match(r"^.+@.+\..+$", text.strip()):
		send_message_api(
			chat_id, token,
			"⚠️ That doesn't look like a valid email. Please try again."
		)
		return

	email = text.strip()
	state.email = email
	state.save(ignore_permissions=True)

	# Look up or create Contact
	ensure_contact(email, telegram_user.full_name)

	# Start collecting fields
	init_field_collection(state, settings)
	ask_next_field(state, chat_id, token)


def ensure_contact(email, full_name):
	"""Look up or create a Contact for the given email."""
	existing = frappe.db.get_value("Contact", {"email_id": email})
	if existing:
		return existing

	# Split name
	parts = (full_name or "").split(" ", 1)
	first_name = parts[0] or email
	last_name = parts[1] if len(parts) > 1 else ""

	contact = frappe.get_doc({
		"doctype": "Contact",
		"first_name": first_name,
		"last_name": last_name,
		"email_ids": [{"email_id": email, "is_primary": 1}],
	})
	contact.insert(ignore_permissions=True)
	return contact.name


# --- Template-driven field collection ---

def init_field_collection(state, settings):
	"""Load template fields and prepare the collection state."""
	# Always collect subject + description
	conversation_fields = [
		{
			"key": "subject",
			"label": "Subject",
			"type": "str",
			"required": True,
			"prompt": "What is your issue about? (brief subject line)",
		},
		{
			"key": "description",
			"label": "Description",
			"type": "str",
			"required": True,
			"prompt": "Please describe the issue in detail.",
		},
	]

	# Add template fields if a template is configured
	if settings.ticket_template:
		try:
			from helpdesk.helpdesk.doctype.hd_ticket_template.api import get_fields_meta

			fields_meta = get_fields_meta(settings.ticket_template)
			for f in fields_meta:
				if f.get("hide_from_customer"):
					continue
				if f.get("fieldname") in ("subject", "description"):
					continue
				conversation_fields.append(map_field_to_meta(f))
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Telegram Helpdesk: template field loading")

	state.state = "collecting_fields"
	state.current_field_index = 0
	state.collected_data = json.dumps({"_fields": conversation_fields})
	state.save(ignore_permissions=True)


def map_field_to_meta(field):
	"""Map an HD Ticket field's metadata to our conversation field format."""
	fieldtype_map = {
		"Data": "str",
		"Small Text": "str",
		"Text": "str",
		"Text Editor": "str",
		"Select": "select",
		"Link": "str",  # Will be overridden below if options fetched
		"Int": "int",
		"Float": "float",
	}
	meta = {
		"key": field.get("fieldname"),
		"label": field.get("label", field.get("fieldname")),
		"type": fieldtype_map.get(field.get("fieldtype"), "str"),
		"required": bool(field.get("required")),
		"prompt": field.get("placeholder") or f"Please provide {field.get('label', field.get('fieldname'))}",
	}
	
	# Handle Select fields with hardcoded options
	if field.get("fieldtype") == "Select" and field.get("options"):
		meta["options"] = field["options"]
		meta["type"] = "select"
	
	# Handle Link fields - fetch options from linked doctype
	elif field.get("fieldtype") == "Link" and field.get("options"):
		linked_doctype = field["options"]
		try:
			# Fetch records from linked doctype
			# For HD Ticket Status, only show enabled statuses
			filters = {}
			if linked_doctype == "HD Ticket Status":
				filters["enabled"] = 1
			
			# Get all records — for priorities, show highest first
			order_by = "name"
			if linked_doctype == "HD Ticket Priority":
				order_by = "integer_value desc"

			records = frappe.get_all(
				linked_doctype,
				filters=filters,
				fields=["name"],
				order_by=order_by
			)
			
			if records:
				# Convert to newline-separated options
				options = "\n".join([r.name for r in records])
				meta["options"] = options
				meta["type"] = "select"
		except Exception:
			# If doctype doesn't exist or error fetching, log and keep as str
			frappe.log_error(
				f"Could not fetch options for Link field {field.get('fieldname')} "
				f"from doctype {linked_doctype}",
				"Telegram Helpdesk: Link field options"
			)
	
	return meta


def ask_next_field(state, chat_id, token):
	"""Ask the user for the next field in the template."""
	data = json.loads(state.collected_data or "{}")
	fields = data.get("_fields", [])

	if state.current_field_index >= len(fields):
		return

	field = fields[state.current_field_index]
	reply_markup = None

	if field.get("type") == "select" and field.get("options"):
		options = [o for o in field["options"].split("\n") if o.strip()]
		if options:
			keyboard = {"inline_keyboard": [[{"text": opt, "callback_data": opt}] for opt in options]}
			reply_markup = keyboard

	optional_hint = "" if field.get("required") else " (optional, send /skip to skip)"
	prompt = f"📝 {field['prompt']}{optional_hint}"

	send_message_api(chat_id, token, prompt, reply_markup=reply_markup)


def handle_field_input(text, telegram_user, telegram_chat, chat_id, token, settings, state):
	"""Process a user's response to a field prompt."""
	# Ensure we have text input (handle None or empty strings)
	if not text or not text.strip():
		send_message_api(chat_id, token, "⚠️ Please provide a valid input.")
		return
	
	data = json.loads(state.collected_data or "{}")
	fields = data.get("_fields", [])
	idx = state.current_field_index

	if idx >= len(fields):
		# All fields collected, prompt for attachments
		prompt_attachment_or_review(state, telegram_user, telegram_chat, chat_id, token, settings)
		return

	current_field = fields[idx]

	# Handle /skip for optional fields
	if text == "/skip" and not current_field.get("required"):
		data[current_field["key"]] = ""
		state.current_field_index = idx + 1
		state.collected_data = json.dumps(data)
		state.save(ignore_permissions=True)

		if state.current_field_index >= len(fields):
			prompt_attachment_or_review(state, telegram_user, telegram_chat, chat_id, token, settings)
		else:
			ask_next_field(state, chat_id, token)
		return

	# Validate required
	if current_field.get("required") and not text.strip():
		send_message_api(chat_id, token, "⚠️ This field is required. Please try again.")
		return

	# Validate select
	if current_field.get("type") == "select" and current_field.get("options"):
		valid_options = [o.strip() for o in current_field["options"].split("\n") if o.strip()]
		if text.strip() not in valid_options:
			send_message_api(chat_id, token, "⚠️ Please select from the options provided.")
			return

	# Validate int/float
	if current_field.get("type") == "int":
		try:
			int(text.strip())
		except ValueError:
			send_message_api(chat_id, token, "⚠️ Please enter a valid number.")
			return

	if current_field.get("type") == "float":
		try:
			float(text.strip())
		except ValueError:
			send_message_api(chat_id, token, "⚠️ Please enter a valid number.")
			return

	# Store the value
	data[current_field["key"]] = text.strip()
	state.current_field_index = idx + 1
	state.collected_data = json.dumps(data)
	state.save(ignore_permissions=True)

	# Check if all fields collected
	if state.current_field_index >= len(fields):
		try:
			prompt_attachment_or_review(state, telegram_user, telegram_chat, chat_id, token, settings)
		except Exception as e:
			frappe.log_error(frappe.get_traceback(), "Telegram Helpdesk: prompt_attachment error")
			send_message_api(chat_id, token, f"❌ Error: {str(e)}. Please try again.")
	else:
		ask_next_field(state, chat_id, token)


# --- Attachment prompt (before review) ---

def prompt_attachment_or_review(state, telegram_user, telegram_chat, chat_id, token, settings):
	"""Ask user if they want to attach files before going to the review screen."""
	keyboard = {
		"inline_keyboard": [
			[{"text": "📎 Attach Document", "callback_data": "attach_document"}],
			[{"text": "⏭ Skip", "callback_data": "skip_to_review"}],
		]
	}
	state.state = "reviewing_ticket"
	state.save(ignore_permissions=True)
	send_message_api(
		chat_id, token,
		"📎 Would you like to attach any files to your ticket?",
		reply_markup=keyboard,
	)


# --- Ticket review ---

def _escape_markdown(text):
	"""Escape Telegram Markdown special characters in user-provided text."""
	for ch in ("*", "_", "`", "["):
		text = text.replace(ch, "\\" + ch)
	return text


def show_ticket_review(state, telegram_user, telegram_chat, chat_id, token, settings):
	"""Show ticket review screen with all collected fields."""
	try:
		data = json.loads(state.collected_data or "{}")
		fields = data.get("_fields", [])

		if not fields:
			send_message_api(chat_id, token, "❌ Error: No fields found. Please start over with /start")
			reset_conversation(state)
			return

		# Build review message
		review_lines = ["📋 *TICKET REVIEW*"]

		for field in fields:
			key = field.get("key")
			label = field.get("label", key)
			value = data.get(key, "")

			if value:
				# Format value - handle long descriptions
				display_value = _escape_markdown(value)
				if len(display_value) > 100:
					display_value = display_value[:100] + "..."
				review_lines.append(f"\n*{_escape_markdown(label)}:* {display_value}")
			else:
				review_lines.append(f"\n*{_escape_markdown(label)}:* None")

		# Show attachment info
		attachments = data.get("_attachments", [])
		if attachments:
			filenames = []
			for file_name in attachments:
				fname = frappe.db.get_value("File", file_name, "file_name")
				if fname:
					filenames.append(_escape_markdown(fname))
			review_lines.append(f"\n*Attachments ({len(attachments)}):* {', '.join(filenames)}")

		review_message = "\n".join(review_lines)

		# Create inline keyboard with Submit, Edit, Attach, Cancel buttons
		attach_label = f"📎 Attach Document ({len(attachments)})" if attachments else "📎 Attach Document"
		keyboard = {
			"inline_keyboard": [
				[{"text": "✅ Submit", "callback_data": "submit_ticket"}],
				[{"text": "✏️ Edit", "callback_data": "edit_ticket"}],
				[{"text": attach_label, "callback_data": "attach_document"}],
				[{"text": "❌ Cancel", "callback_data": "cancel_ticket"}],
			]
		}

		state.state = "reviewing_ticket"
		state.save(ignore_permissions=True)

		send_message_api(chat_id, token, review_message, reply_markup=keyboard, parse_mode="Markdown")
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Telegram Helpdesk: show_ticket_review error")
		send_message_api(chat_id, token, f"❌ Error preparing review: {str(e)}. Please try again.")
		return


def show_edit_field_menu(state, chat_id, token):
	"""Show menu to select which field to edit."""
	data = json.loads(state.collected_data or "{}")
	fields = data.get("_fields", [])

	# Create buttons for each field
	keyboard_buttons = []
	for field in fields:
		key = field.get("key")
		label = field.get("label", key)
		keyboard_buttons.append([{"text": label, "callback_data": f"edit_field_{key}"}])

	keyboard = {"inline_keyboard": keyboard_buttons}

	send_message_api(chat_id, token, "✏️ Which field would you like to change?", reply_markup=keyboard)


def handle_edit_field(field_key, telegram_user, telegram_chat, chat_id, token, settings, state):
	"""Start editing a specific field."""
	data = json.loads(state.collected_data or "{}")
	fields = data.get("_fields", [])

	# Find the field
	field = None
	for f in fields:
		if f.get("key") == field_key:
			field = f
			break

	if not field:
		send_message_api(chat_id, token, "❌ Field not found. Please try again.")
		show_ticket_review(state, telegram_user, telegram_chat, chat_id, token, settings)
		return

	# Store which field we're editing
	state.state = "editing_field"
	state.collected_data = json.dumps({**data, "_editing_field": field_key})
	state.save(ignore_permissions=True)

	# Show current value and ask for new value
	current_value = data.get(field_key, "")
	if current_value:
		current_text = f"\n\n*Current value:* {_escape_markdown(current_value)}"
	else:
		current_text = "\n\n*Current value:* _(not set)_"
	
	reply_markup = None
	if field.get("type") == "select" and field.get("options"):
		options = [o for o in field["options"].split("\n") if o.strip()]
		if options:
			keyboard = {"inline_keyboard": [[{"text": opt, "callback_data": opt}] for opt in options]}
			reply_markup = keyboard

	optional_hint = "" if field.get("required") else " (optional, send /skip to skip)"
	prompt = f"{field['prompt']}{optional_hint}{current_text}"

	send_message_api(chat_id, token, prompt, reply_markup=reply_markup, parse_mode="Markdown")


def handle_editing_field_input(text, telegram_user, telegram_chat, chat_id, token, settings, state):
	"""Handle input while editing a field."""
	data = json.loads(state.collected_data or "{}")
	fields = data.get("_fields", [])
	editing_field_key = data.get("_editing_field")

	if not editing_field_key:
		show_ticket_review(state, telegram_user, telegram_chat, chat_id, token, settings)
		return

	# Find the field being edited
	field = None
	for f in fields:
		if f.get("key") == editing_field_key:
			field = f
			break

	if not field:
		show_ticket_review(state, telegram_user, telegram_chat, chat_id, token, settings)
		return

	# Handle /skip for optional fields
	if text == "/skip" and not field.get("required"):
		data[editing_field_key] = ""
		data.pop("_editing_field", None)
		state.state = "reviewing_ticket"
		state.collected_data = json.dumps(data)
		state.save(ignore_permissions=True)
		show_ticket_review(state, telegram_user, telegram_chat, chat_id, token, settings)
		return

	# Validate required
	if field.get("required") and not text.strip():
		send_message_api(chat_id, token, "⚠️ This field is required. Please try again.")
		return

	# Validate select
	if field.get("type") == "select" and field.get("options"):
		valid_options = [o.strip() for o in field["options"].split("\n") if o.strip()]
		if text.strip() not in valid_options:
			send_message_api(chat_id, token, "⚠️ Please select from the options provided.")
			return

	# Validate int/float
	if field.get("type") == "int":
		try:
			int(text.strip())
		except ValueError:
			send_message_api(chat_id, token, "⚠️ Please enter a valid number.")
			return

	if field.get("type") == "float":
		try:
			float(text.strip())
		except ValueError:
			send_message_api(chat_id, token, "⚠️ Please enter a valid number.")
			return

	# Update the field value
	data[editing_field_key] = text.strip()
	data.pop("_editing_field", None)
	state.state = "reviewing_ticket"
	state.collected_data = json.dumps(data)
	state.save(ignore_permissions=True)

	# Show updated review
	show_ticket_review(state, telegram_user, telegram_chat, chat_id, token, settings)


# --- Attachment handling ---

def handle_attach_document_start(state, chat_id, token):
	"""Prompt the user to send files."""
	state.state = "awaiting_attachment"
	state.save(ignore_permissions=True)

	data = json.loads(state.collected_data or "{}")
	count = len(data.get("_attachments", []))
	count_msg = f"\n{count} file(s) attached so far." if count else ""

	keyboard = {
		"inline_keyboard": [
			[{"text": "✅ Done", "callback_data": "done_attaching"}],
		]
	}
	send_message_api(
		chat_id, token,
		f"📎 Send me a document, photo, or video to attach to your ticket.{count_msg}\n\nPress Done when finished.",
		reply_markup=keyboard,
	)


def handle_attachment_upload(message, state, chat_id, token):
	"""Process a file upload during the awaiting_attachment state."""
	if not message:
		return

	# Extract file_id from document, photo, or video
	file_id = None
	file_name = None

	if message.get("document"):
		file_id = message["document"]["file_id"]
		file_name = message["document"].get("file_name", "document")
	elif message.get("photo"):
		# Photos come as an array of sizes; pick the largest
		file_id = message["photo"][-1]["file_id"]
		file_name = "photo.jpg"
	elif message.get("video"):
		file_id = message["video"]["file_id"]
		file_name = message["video"].get("file_name", "video.mp4")

	if not file_id:
		send_message_api(chat_id, token, "⚠️ Please send a document, photo, or video.")
		return

	# Download from Telegram
	try:
		settings = frappe.get_cached_doc("Helpdesk Telegram Settings")
		bot_doc = frappe.get_doc("Telegram Bot", settings.bot)
		bot_token = bot_doc.get_password("api_token")
	except Exception:
		send_message_api(chat_id, token, "❌ Error: could not retrieve bot token.")
		return

	tg_file_path = get_file_info(file_id, bot_token)
	if not tg_file_path:
		send_message_api(chat_id, token, "❌ Error retrieving file info from Telegram. Please try again.")
		return

	file_bytes = download_telegram_file(tg_file_path, bot_token)
	if not file_bytes:
		send_message_api(chat_id, token, "❌ Error downloading file. Please try again.")
		return

	# Save as a private Frappe File (unattached for now)
	file_doc = save_file_to_disk(file_name, file_bytes, "", "", is_private=1)
	frappe.db.commit()

	# Store in collected_data._attachments
	data = json.loads(state.collected_data or "{}")
	attachments = data.get("_attachments", [])
	attachments.append(file_doc.name)
	data["_attachments"] = attachments
	state.collected_data = json.dumps(data)
	state.save(ignore_permissions=True)

	keyboard = {
		"inline_keyboard": [
			[{"text": "✅ Done", "callback_data": "done_attaching"}],
		]
	}
	send_message_api(
		chat_id, token,
		f"✅ File '{file_name}' attached. ({len(attachments)} total)\nSend more or press Done.",
		reply_markup=keyboard,
	)


def handle_submit_ticket(telegram_user, telegram_chat, chat_id, token, settings, state):
	"""Submit the ticket after review."""
	try:
		data = json.loads(state.collected_data or "{}")
		create_ticket(data, telegram_user, telegram_chat, chat_id, token, settings, state)
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Telegram Helpdesk: submit_ticket error")
		send_message_api(chat_id, token, f"❌ Error submitting ticket: {str(e)}. Please try again.")


# --- Ticket creation ---

def create_ticket(data, telegram_user, telegram_chat, chat_id, token, settings, state):
	"""Create an HD Ticket with the collected data."""
	email = state.email

	ticket_values = {
		"doctype": "HD Ticket",
		"subject": data.get("subject", "Telegram Support Request"),
		"description": data.get("description", data.get("subject", "")),
		"raised_by": email,
		"via_customer_portal": 1,
		"custom_source": "Telegram",
		"custom_telegram_user_id": str(telegram_user.telegram_user_id),
		"custom_telegram_username": telegram_user.telegram_username or "",
	}

	if settings.default_ticket_type:
		ticket_values["ticket_type"] = settings.default_ticket_type
	if settings.default_agent_group:
		ticket_values["agent_group"] = settings.default_agent_group
	if settings.ticket_template:
		ticket_values["template"] = settings.ticket_template

	# Add template-collected fields
	for key, value in data.items():
		if key.startswith("_") or key in ("subject", "description") or not value:
			continue
		# Try direct field, then custom_ prefixed
		if key in frappe.get_meta("HD Ticket").get_fieldnames_with_value():
			ticket_values[key] = value
		elif f"custom_{key}" in frappe.get_meta("HD Ticket").get_fieldnames_with_value():
			ticket_values[f"custom_{key}"] = value

	try:
		ticket_doc = frappe.get_doc(ticket_values)
		ticket_doc.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception as e:
		error_msg = str(e)
		frappe.log_error(frappe.get_traceback(), "Telegram Helpdesk: ticket creation")
		send_message_api(chat_id, token, f"❌ Sorry, there was an error creating your ticket: {error_msg[:200]}. Please try again.")
		reset_conversation(state)
		return

	# Link uploaded attachments to the ticket
	for file_name in data.get("_attachments", []):
		if frappe.db.exists("File", file_name):
			file_doc = frappe.get_doc("File", file_name)
			file_doc.attached_to_doctype = "HD Ticket"
			file_doc.attached_to_name = ticket_doc.name
			file_doc.save(ignore_permissions=True)
	frappe.db.commit()

	# Create mapping for two-way communication
	frappe.get_doc({
		"doctype": "Helpdesk Telegram Ticket",
		"telegram_user": telegram_user.name,
		"telegram_chat": telegram_chat.name,
		"ticket": ticket_doc.name,
		"is_open": 1,
	}).insert(ignore_permissions=True)

	# Reset conversation state
	reset_conversation(state)

	# Management notifications
	try:
		from frappe_telegram.handlers.helpdesk_notifications import notify_ticket_created
		notify_ticket_created(ticket_doc.name, telegram_user.name)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Telegram Helpdesk: notification error")

	# Send confirmation to Telegram user
	try:
		msg = frappe.render_template(
			settings.ticket_created_message or "Ticket #{{ ticket.name }} created: {{ ticket.subject }}",
			{"ticket": ticket_doc},
		)
	except Exception:
		msg = f"\u2705 Ticket #{ticket_doc.name} created: {ticket_doc.subject}"

	send_message_api(chat_id, token, msg)


# --- Reopen ticket ---

def handle_reopen_ticket(callback_data, telegram_user, chat_id, token):
	"""Reopen a resolved ticket."""
	ticket_name = callback_data.replace("reopen_ticket_", "")

	# Verify ticket exists and belongs to this user
	mapping = frappe.db.get_value(
		"Helpdesk Telegram Ticket",
		{"ticket": ticket_name, "telegram_user": telegram_user.name},
		"name",
	)
	if not mapping:
		send_message_api(chat_id, token, "❌ Ticket not found or does not belong to you.")
		return

	try:
		ticket = frappe.get_doc("HD Ticket", ticket_name)
		ticket.status = "Re-Open"
		ticket.flags.skip_telegram_notify = True
		ticket.flags.ignore_version = True
		ticket.save(ignore_permissions=True)

		frappe.db.set_value("Helpdesk Telegram Ticket", mapping, "is_open", 1)
		frappe.db.commit()

		# Management notification — enqueue after commit to avoid
		# TimestampMismatch race condition with the ticket save above
		frappe.enqueue(
			method="frappe_telegram.handlers.helpdesk_notifications.notify_ticket_reopened",
			queue="short",
			ticket_name=ticket_name,
			telegram_user_name=telegram_user.name,
			enqueue_after_commit=True,
		)

		# Rich Telegram message to user (synchronous — only reads data)
		from frappe_telegram.handlers.helpdesk_notifications import (
			build_rich_status_reopened_message,
		)
		msg = build_rich_status_reopened_message(ticket_name)
		send_message_api(chat_id, token, msg, parse_mode="HTML")
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Telegram Helpdesk: reopen ticket")
		send_message_api(chat_id, token, f"\u274c Error reopening ticket: {str(e)[:200]}")


# --- My Tickets ---

def handle_my_tickets(telegram_user, chat_id, token):
	"""Show user's open tickets."""
	mappings = frappe.get_all(
		"Helpdesk Telegram Ticket",
		filters={"telegram_user": telegram_user.name, "is_open": 1},
		fields=["ticket"],
	)

	if not mappings:
		send_message_api(chat_id, token, "📭 You have no open tickets. Tap /start to create one.")
		return

	lines = ["📋 Your open tickets:\n"]
	for m in mappings:
		ticket = frappe.db.get_value(
			"HD Ticket", m.ticket,
			["name", "subject", "status"], as_dict=True,
		)
		if ticket:
			lines.append(f"🎫 #{ticket.name} - {ticket.subject} ({ticket.status})")

	send_message_api(chat_id, token, "\n".join(lines))


# --- Follow-up messages ---

def handle_followup_or_prompt(text, telegram_user, telegram_chat, chat_id, token, message=None):
	"""Handle a message that's not part of a ticket creation conversation."""
	# Determine if the message contains an attachment
	has_attachment = message and (
		message.get("document") or message.get("photo") or message.get("video")
	)

	if not text and not has_attachment:
		return

	# Check for open ticket mapping
	mapping = frappe.db.get_value(
		"Helpdesk Telegram Ticket",
		{"telegram_user": telegram_user.name, "is_open": 1},
		["name", "ticket"],
		as_dict=True,
	)

	if mapping:
		ticket = frappe.get_doc("HD Ticket", mapping.ticket)
		# Get email for sender
		state = frappe.db.get_value(
			"Telegram Conversation State",
			{"telegram_user": telegram_user.name},
			"email",
		)
		sender = state or telegram_user.full_name

		# Download and save attachment if present
		attachment_file = None
		if has_attachment:
			attachment_file = _download_followup_attachment(message, chat_id, token)

		# Build communication content
		content = text or ""
		if attachment_file and not content:
			content = f"[Attachment: {attachment_file.file_name}]"

		comm = frappe.get_doc({
			"doctype": "Communication",
			"communication_type": "Communication",
			"content": content,
			"reference_doctype": "HD Ticket",
			"reference_name": mapping.ticket,
			"sender": sender,
			"sent_or_received": "Received",
			"subject": f"Re: {ticket.subject}",
		}).insert(ignore_permissions=True)

		# Link attachment to the HD Ticket so it's visible in the helpdesk
		if attachment_file:
			attachment_file.attached_to_doctype = "HD Ticket"
			attachment_file.attached_to_name = mapping.ticket
			attachment_file.save(ignore_permissions=True)
			frappe.db.commit()

		# Management notifications + rich confirmation
		try:
			from frappe_telegram.handlers.helpdesk_notifications import (
				notify_user_response,
				build_rich_followup_confirmation,
			)
			preview = text or (f"[Attachment: {attachment_file.file_name}]" if attachment_file else "")
			notify_user_response(mapping.ticket, telegram_user.name, preview)
			msg = build_rich_followup_confirmation(mapping.ticket)
			send_message_api(chat_id, token, msg, parse_mode="HTML")
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Telegram Helpdesk: notification error")
			send_message_api(chat_id, token, f"\u2705 Message added to ticket #{mapping.ticket}")
	else:
		send_message_api(chat_id, token, "💬 No open ticket found. Send /start to see options.")


def _download_followup_attachment(message, chat_id, token):
	"""Download a Telegram file attachment and save as a private Frappe File."""
	file_id = None
	file_name = None

	if message.get("document"):
		file_id = message["document"]["file_id"]
		file_name = message["document"].get("file_name", "document")
	elif message.get("photo"):
		file_id = message["photo"][-1]["file_id"]
		file_name = "photo.jpg"
	elif message.get("video"):
		file_id = message["video"]["file_id"]
		file_name = message["video"].get("file_name", "video.mp4")

	if not file_id:
		return None

	try:
		settings = frappe.get_cached_doc("Helpdesk Telegram Settings")
		bot_doc = frappe.get_doc("Telegram Bot", settings.bot)
		bot_token = bot_doc.get_password("api_token")
	except Exception:
		return None

	tg_file_path = get_file_info(file_id, bot_token)
	if not tg_file_path:
		return None

	file_bytes = download_telegram_file(tg_file_path, bot_token)
	if not file_bytes:
		return None

	file_doc = save_file_to_disk(file_name, file_bytes, "", "", is_private=1)
	frappe.db.commit()
	return file_doc
